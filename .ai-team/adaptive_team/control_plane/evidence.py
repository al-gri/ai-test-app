"""Immutable encrypted display evidence, registered only by trusted maintenance.

The browser cannot select repository paths, refs, Git options or arbitrary files.
A displayed diff is bound to the exact subject and Inbox binding, not 'latest'.
"""
import json
from contextlib import closing
from dataclasses import asdict
from ..models import PolicyError, canonical, digest
from ..security.payload_crypto import PayloadCipher
from .queries import read


class EvidenceStore:
    def __init__(self, team, cipher=None):
        self.team, self.cipher = team, cipher or PayloadCipher()
        with closing(team._connect()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ui_evidence(
                request_id TEXT PRIMARY KEY REFERENCES human_requests(id),
                binding TEXT NOT NULL, subject TEXT NOT NULL, display_digest TEXT NOT NULL,
                encrypted_document TEXT NOT NULL)''')

    def _aad(self, request_id):
        return {'project':self.team.snapshot()['project'],'operation':request_id,'field':'ui_evidence'}

    def _put(self, item, document):
        value = {'request_id':item['id'],'binding':item['binding'],
                 'project':item['project'],'task_id':item['task_id'],
                 'subject_digest':item['subject_digest'], **document}
        encoded = canonical(value)
        if len(encoded.encode()) > 600000:
            raise PolicyError('Review evidence exceeds bound; split the candidate')
        checksum = digest(value)
        encrypted = self.cipher.encrypt(value,self._aad(item['id']))
        with closing(self.team._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT document FROM human_requests WHERE id=?',(item['id'],)).fetchone()
            if not current or json.loads(current[0]) != item or item['status']!='pending':
                raise PolicyError('Inbox changed while collecting review evidence')
            old = db.execute('SELECT display_digest FROM ui_evidence WHERE request_id=?',(item['id'],)).fetchone()
            if old and old[0]!=checksum:
                raise PolicyError('Display evidence is immutable; replace the Inbox request')
            db.execute('INSERT INTO ui_evidence VALUES(?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING',
                (item['id'],item['binding'],item['subject_digest'],checksum,encrypted))
            db.commit()
        return checksum

    def _item(self, request_id):
        with read(self.team.database) as db:
            row=db.execute('SELECT document FROM human_requests WHERE id=?',(request_id,)).fetchone()
            if not row:raise PolicyError('Unknown Inbox request')
            return json.loads(row[0])

    def register_candidate(self, request_id, executor):
        """Owner adapter calls after creating the hold, with its trusted executor."""
        item=self._item(request_id)
        candidate, contract=executor.candidate(item['subject_digest'])
        if contract.project!=item['project'] or contract.ticket['task_id']!=item['task_id']:
            raise PolicyError('Candidate belongs to another request')
        git=executor.commits.git
        repo=executor.commits.worktrees.repository
        # Candidate verification pins immutable object IDs. Never diff live HEAD.
        raw=git.run(repo,'diff','--no-ext-diff','--no-textconv','--no-renames',
                    '--no-color','--full-index',candidate.base_commit,candidate.commit,'--')
        if len(raw)>512000:raise PolicyError('Diff too large; split the candidate')
        text=raw.decode('utf-8','strict')
        binary=b'Binary files ' in raw or b'GIT binary patch' in raw
        return self._put(item,{'kind':'git','base':candidate.base_commit,'target':candidate.commit,
            'repository_id':digest(str(repo.resolve())), 'candidate':asdict(candidate),
            'diff':text,'diff_digest':digest(text),'complete':not binary,
            'notice':'Binary change requires separate review; approval disabled' if binary else 'Complete textual diff'})

    def register_document(self, request_id, document):
        """Owner-approved plan/recovery evidence; its digest must be the subject."""
        item=self._item(request_id)
        if digest(document)!=item['subject_digest']:
            raise PolicyError('Document does not match requested subject')
        return self._put(item,{'kind':'document','document':document,'complete':True,
                              'notice':'Structured non-code decision; no Git diff'})

    def display(self, request_id):
        with read(self.team.database) as db:
            row=db.execute('SELECT * FROM ui_evidence WHERE request_id=?',(request_id,)).fetchone()
        if not row:raise PolicyError('Trusted review evidence has not been registered')
        value=self.cipher.decrypt(row['encrypted_document'],self._aad(request_id))
        if digest(value)!=row['display_digest']:raise PolicyError('Review evidence integrity failure')
        return {**value,'display_digest':row['display_digest']}

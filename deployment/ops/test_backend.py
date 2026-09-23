import copy
import unittest
from deepseek_backend import FILES, parse_files, router
from adaptive_team.models import PolicyError
from adaptive_team.providers.router import RoutingMetadata
from adaptive_team.llmops.token_ledger import charge

class ProposalBoundary(unittest.TestCase):
    def good(self): return {'files':[{'path':p,'content':'data'} for p in sorted(FILES)]}
    def test_complete(self): self.assertEqual(set(parse_files(self.good())),FILES)
    def test_traversal_and_control_paths(self):
        for path in ('../credentials/key','app/../x','.ai-team/config.json','app/link','/etc/passwd'):
            value=self.good(); value['files'][0]['path']=path
            with self.assertRaises(PolicyError):parse_files(value)
    def test_duplicate(self):
        value=self.good();value['files'].append(copy.deepcopy(value['files'][0]))
        with self.assertRaises(PolicyError):parse_files(value)
    def test_size_limit(self):
        value=self.good();value['files'][0]['content']='x'*64001
        with self.assertRaises(PolicyError):parse_files(value)
    def test_missing(self):
        value=self.good();value['files'].pop()
        with self.assertRaises(PolicyError):parse_files(value)
    def test_pinned_model_and_price_upper_bound(self):
        profile=router().route(RoutingMetadata('code_review')).profile
        self.assertEqual(profile.model,'deepseek-flash')
        self.assertEqual(charge(96000,12000,(profile.input_price,profile.output_price)),43200)

class Selection(unittest.TestCase):
    def test_default_recovery_and_explicit_exhausted_rejection(self):
        from run_project import select_task
        state={'tasks':{'calculator':{'status':'escalated'},'calculator-recovery':{'status':'pending'}}}
        self.assertEqual(select_task(state),'calculator-recovery')
        for selected in ('calculator','missing'):
            with self.assertRaises(PolicyError):select_task(state,selected)

if __name__=='__main__':unittest.main(verbosity=2)

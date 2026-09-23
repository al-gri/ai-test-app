"""Non-negotiable absolute quality floor before relative cost/quality gates."""
from dataclasses import dataclass
from decimal import Decimal
import math
from ..models import PolicyError


@dataclass(frozen=True)
class QualityPolicy:
    minimum_score: float = 0.8

    def __post_init__(self):
        if type(self.minimum_score) not in (int, float) or not math.isfinite(self.minimum_score) or not .8 <= self.minimum_score <= 1:
            raise PolicyError('Owner quality floor must be between 80% and 100%')

    def evaluate(self, cases):
        if not isinstance(cases, list) or not cases:
            raise PolicyError('Measured benchmark cases required')
        scores = []
        for case in cases:
            if not isinstance(case, dict): raise PolicyError('Invalid quality case')
            value = case.get('candidate')
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise PolicyError('Invalid absolute quality score')
            if case.get('critical_pass') is not True:
                raise PolicyError('Every mandatory critical check must pass')
            scores.append(Decimal(str(value)))
        average = sum(scores) / len(scores)
        if average < Decimal(str(self.minimum_score)):
            raise PolicyError('Candidate fails absolute quality floor')
        return {'minimum_score': self.minimum_score, 'score': float(average), 'critical_pass': True}

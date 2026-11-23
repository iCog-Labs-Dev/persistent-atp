"""Record scoped proposals before delegating every write to CommitGate."""
from contextlib import contextmanager
import threading

from commit_gate.gate import CommitResult
from commit_gate.reasons import Reason, Rejection

from .bounded_ledger import BoundedLedger, BoundedBlocked


class BoundedGate:
    def __init__(self,gate,service):
        self.gate,self.service,self.local=gate,service,threading.local()

    def __getattr__(self,name):return getattr(self.gate,name)

    @contextmanager
    def scope(self,operation):
        previous=getattr(self.local,'operation',None)
        self.local.operation=operation
        try:yield
        finally:self.local.operation=previous

    def commit(self,proposal):
        with self.service.lock:
            ledger=BoundedLedger(self.service.jobs)
            owner=ledger.owner(proposal.proof_id)
            if owner:
                child=getattr(self.local,'operation',None)
                if ledger.child_owner(child)!=owner:
                    return CommitResult(False,(Rejection(Reason.BOUNDED_TASK_OWNERSHIP,
                        'proof is reserved by a bounded refinement task'),),None,None)
                try:ledger.record(owner,child,proposal.to_dict())
                except BoundedBlocked as error:
                    return CommitResult(False,(Rejection(Reason.BOUNDED_TASK_OWNERSHIP,str(error)),),None,None)
            return self.gate.commit(proposal)

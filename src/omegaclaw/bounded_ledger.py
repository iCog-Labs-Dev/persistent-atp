"""Atomic receiver-owned allowances. Unknown work keeps its full reservation."""
from contextlib import contextmanager
import json

from commit_gate.canon import canonical_json, content_hash


class BoundedBlocked(ValueError):
    code='BOUNDED_REFINEMENT_REQUIRES_RECONCILIATION'
    def __init__(self,message):
        super().__init__(message)
        self.public_summary=message


def create_tables(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS bounded_owners (proof TEXT PRIMARY KEY, operation TEXT NOT NULL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS bounded_steps (
        task TEXT NOT NULL, step TEXT NOT NULL, child TEXT NOT NULL UNIQUE, body_hash TEXT NOT NULL, request_body TEXT NOT NULL,
        search_cap INTEGER NOT NULL, replay_cap INTEGER NOT NULL, status TEXT NOT NULL,
        search_used INTEGER, replay_used INTEGER, outcome TEXT, PRIMARY KEY(task,step))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS bounded_writes (
        task TEXT NOT NULL, child TEXT NOT NULL, digest TEXT NOT NULL, proposal TEXT NOT NULL,
        PRIMARY KEY(task,child,digest))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS bounded_audit (
        sequence INTEGER PRIMARY KEY, task TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)''')


class BoundedLedger:
    def __init__(self,jobs):self.conn=jobs.conn

    @contextmanager
    def transaction(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK');raise

    def audit(self,task,kind,data):
        self.conn.execute('INSERT INTO bounded_audit(task,kind,payload) VALUES (?,?,?)',
            (task,kind,canonical_json(data).decode()))

    def owner(self,proof):
        row=self.conn.execute('SELECT operation FROM bounded_owners WHERE proof=?',(proof,)).fetchone()
        return row[0] if row else None

    def state(self,task):
        row=self.conn.execute('SELECT bounded_json FROM operations WHERE operation_id=?',(task,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def _save(self,task,state):
        if self.conn.execute('UPDATE operations SET bounded_json=? WHERE operation_id=?',
                (canonical_json(state).decode(),task)).rowcount!=1:
            raise BoundedBlocked('bounded refinement requires an admitted operation')

    def start(self,task,state):
        with self.transaction():
            old=self.state(task)
            if old:
                if old['request']!=state['request']:raise BoundedBlocked('bounded task request changed')
                return old
            owner=self.owner(state['proof_id'])
            if owner and owner!=task:raise BoundedBlocked('another bounded task owns this proof')
            self.conn.execute('INSERT OR IGNORE INTO bounded_owners VALUES (?,?)',(state['proof_id'],task))
            self._save(task,state);self.audit(task,'admitted',state['request'])
            return state

    def update(self,task,**patch):
        with self.transaction():
            state=self.state(task)
            if not state:raise BoundedBlocked('bounded task state is missing')
            state.update(patch);self._save(task,state)
        return state

    def child_owner(self,child):
        row=self.conn.execute('SELECT task FROM bounded_steps WHERE child=?',(child,)).fetchone()
        return row[0] if row else None

    def step(self,task,name):
        row=self.conn.execute('SELECT * FROM bounded_steps WHERE task=? AND step=?',(task,name)).fetchone()
        return dict(row) if row else None

    def usage(self,task):
        rows=self.conn.execute('SELECT * FROM bounded_steps WHERE task=?',(task,)).fetchall()
        return {kind:sum(row[kind+'_used'] if row['status']=='settled' else row[kind+'_cap'] for row in rows)
                for kind in ('search','replay')}

    def reserve(self,task,name,child,request,search,replay):
        with self.transaction():
            state=self.state(task);old=self.step(task,name)
            digest=content_hash(request)
            if old:
                if (old['child'],old['body_hash'],old['search_cap'],old['replay_cap'])!=(child,digest,search,replay):
                    raise BoundedBlocked('saved bounded step binding changed')
                return old
            if self.owner(state['proof_id'])!=task:raise BoundedBlocked('bounded task lost proof ownership')
            used=self.usage(task);policy=state['request']['policy']
            if used['search']+search>policy['total_search_steps'] or used['replay']+replay>policy['total_replay_steps']:
                raise BoundedBlocked('CUMULATIVE_BUDGET_EXHAUSTED')
            self.conn.execute('INSERT INTO bounded_steps VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL)',
                (task,name,child,digest,canonical_json(request).decode(),search,replay,'reserved'))
            self.audit(task,'reserved',{'step':name,'child':child,'search':search,'replay':replay,'body_hash':digest})
            return self.step(task,name)

    def record(self,task,child,proposal):
        with self.transaction():
            row=self.conn.execute('SELECT * FROM bounded_steps WHERE task=? AND child=?',(task,child)).fetchone()
            state=self.state(task)
            if not row or row['status']!='reserved' or self.owner(state['proof_id'])!=task:
                raise BoundedBlocked('graph write has no active bounded reservation')
            self.conn.execute('INSERT OR IGNORE INTO bounded_writes VALUES (?,?,?,?)',
                (task,child,content_hash(proposal),canonical_json(proposal).decode()))

    def prepared(self,task):
        return [json.loads(row[0]) for row in self.conn.execute('SELECT proposal FROM bounded_writes WHERE task=?',(task,))]

    def settle(self,task,name,outcome,search,replay):
        with self.transaction():
            row=self.step(task,name)
            if not row:raise BoundedBlocked('settlement has no reservation')
            if not 0<=search<=row['search_cap'] or not 0<=replay<=row['replay_cap']:
                raise BoundedBlocked('reported work exceeds its reserved allowance')
            encoded=canonical_json(outcome).decode()
            if row['status']=='settled':
                if (row['outcome'],row['search_used'],row['replay_used'])!=(encoded,search,replay):
                    raise BoundedBlocked('settled bounded evidence changed')
                return
            self.conn.execute("UPDATE bounded_steps SET status='settled',search_used=?,replay_used=?,outcome=? WHERE task=? AND step=?",
                (search,replay,encoded,task,name))
            self.audit(task,'settled',{'step':name,'search':search,'replay':replay})

    def release(self,task):
        with self.transaction():
            if self.conn.execute("SELECT 1 FROM bounded_steps WHERE task=? AND status!='settled'",(task,)).fetchone():
                raise BoundedBlocked('unsettled reservations retain proof ownership')
            self.conn.execute('DELETE FROM bounded_owners WHERE operation=?',(task,))
            self.audit(task,'released',{})

    def finish(self,task,outcome):
        with self.transaction():
            if self.conn.execute("SELECT 1 FROM bounded_steps WHERE task=? AND status!='settled'",(task,)).fetchone():
                raise BoundedBlocked('unsettled reservations retain proof ownership')
            state=self.state(task);state.update(phase='completed',outcome=outcome)
            self._save(task,state)
            self.conn.execute('DELETE FROM bounded_owners WHERE operation=?',(task,))
            self.audit(task,'completed',{'reason':outcome['reason']})

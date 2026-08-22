# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
import json,tempfile,time,unittest
from pathlib import Path
from harness import board,control,scheduler
class SchedulerTests(unittest.TestCase):
 def test_tick_records_due_delivery_without_spawning(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); profile=root/'profile.json'; profile.write_text(json.dumps({"project_name":"x","default_branch":"main","test_command":"t","build_command":"b","health_command":"h","deployment_channels":["local"],"agent_poll_interval_seconds":1,"agent_commands":{}}))
   session=control.create(root,'codex_delivery'); agent=board.register(root,'development',board.AWAITING_OWNER_DIRECTION,session_id=session['id']); board.record_owner_direction(root,session['id'],'T1'); board.begin_task(root,agent['id'],'T1'); board.status(root,agent['id'],'working')
   state=board.snapshot(root); state['agents'][agent['id']]['last_status_at']='2000-01-01T00:00:00+00:00'; (root/'.harness/board/state.json').write_text(json.dumps(state))
   out=scheduler.tick(root,profile); self.assertTrue(out['dispatches']); self.assertFalse(out['dispatches'][0]['spawned'])
 def test_tick_executes_only_profile_approved_command(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); profile=root/'profile.json'; profile.write_text(json.dumps({"project_name":"x","default_branch":"main","test_command":"t","build_command":"b","health_command":"h","deployment_channels":["local"],"agent_poll_interval_seconds":1,"agent_commands":{"OpenAI":"true"}}))
   session=control.create(root,'codex_delivery'); agent=board.register(root,'development',board.AWAITING_OWNER_DIRECTION,vendor='OpenAI',session_id=session['id']); board.record_owner_direction(root,session['id'],'T2'); board.begin_task(root,agent['id'],'T2'); board.status(root,agent['id'],'working')
   state=board.snapshot(root); state['agents'][agent['id']]['last_status_at']='2000-01-01T00:00:00+00:00'; (root/'.harness/board/state.json').write_text(json.dumps(state))
   out=scheduler.tick(root,profile,execute=True); self.assertTrue(out['dispatches'][0]['spawned'])
   for _ in range(20):
    if scheduler.reap_children(): break
    time.sleep(.01)
   self.assertFalse(scheduler._CHILDREN)

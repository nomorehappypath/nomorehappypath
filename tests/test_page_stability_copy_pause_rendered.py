# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Regression from the task review of page-stability-copy (the reviewer's own challenge, kept): a real board Pause then Resume leaves the proposal buttons enabled."""
import json,tempfile,threading,time,unittest
from pathlib import Path
from harness import board
import tests.test_page_stability_copy_rendered as stability
from tests.test_no_terminal_questions_rendered import run_page
PROBE=r'''<script>(async()=>{const wait=ms=>new Promise(r=>setTimeout(r,ms)),out={};try{
for(let i=0;i<120&&!document.querySelector('[data-req-go]');i++)await wait(100);
out.before=!document.querySelector('[data-req-go]').disabled;
for(let i=0;i<120&&!document.body.classList.contains('project-paused');i++)await wait(150);
out.paused=document.body.classList.contains('project-paused');out.disabledWhilePaused=document.querySelector('[data-req-go]').disabled;
for(let i=0;i<120&&document.body.classList.contains('project-paused');i++)await wait(150);
await wait(2500);out.resumed=!document.body.classList.contains('project-paused');out.goEnabled=!document.querySelector('[data-req-go]').disabled;
out.questionButtons=[...document.querySelector('.requirements-proposal').querySelectorAll('button')].map(b=>({text:b.textContent,disabled:b.disabled}));
}catch(e){out.error=String(e)}await fetch('/__probe__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(out)});})();</script>'''
class RealPause(unittest.TestCase):
 def test_proposal_go_ahead_recovers(self):
  with tempfile.TemporaryDirectory() as tmp:
   root,_,_=stability.RenderedPageStabilityTests().fixture(Path(tmp))
   def cycle():
    time.sleep(5);board.begin_project_pause(root,drain_seconds=0);board.finish_project_pause(root)
    time.sleep(6);t=board.begin_project_resume(root);board.finish_project_resume(root,t['resume_id'])
   worker=threading.Thread(target=cycle);worker.start();self.addCleanup(worker.join)
   result=run_page(self,root,PROBE,width=1100,height=600,wait=45)
   print('REAL_PAUSE '+json.dumps(result,sort_keys=True),flush=True)
   self.assertTrue(result.get('before') and result.get('paused') and result.get('resumed'))
   self.assertTrue(result.get('goEnabled'),'Go ahead is still disabled after real board pause/resume')
if __name__=='__main__':unittest.main(verbosity=2)

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Regression from the task review of page-stability-copy (the reviewer's own challenge, kept): held content, drafts through a rewrite, pause then resume on saved snapshots, Copy of proposal, directive and history."""
import copy, json, tempfile, unittest
from pathlib import Path
from harness import browser_acceptance
import tests.test_page_stability_copy_rendered as stability
from tests.test_no_terminal_questions_rendered import run_page
PROBE=r'''<script>
(async()=>{
const out={};const wait=ms=>new Promise(r=>setTimeout(r,ms));
try{
for(let i=0;i<150&&!document.querySelector('[data-cq-other]');i++)await wait(150);
// Freeze network polling while challenging the actual render with successive dashboard snapshots.
refreshing=true;
const data=structuredClone(lastBoard),managed=structuredClone(lastManaged);
const body=document.querySelector('.requirements-confirmation:not(.requirements-proposal) .requirements-body');
const task=body.closest('[data-task]').dataset.task;
const selection=getSelection(),range=document.createRange();range.selectNodeContents(body);selection.removeAllRanges();selection.addRange(range);
const selected=selection.toString();
const next=structuredClone(data);next.requirement_confirmations[task].text='Objective: UPDATED agreement.\nAcceptance: updated requirement.';
const reviewer=Object.values(next.state.agents).find(a=>a.role==='qa'); if(reviewer)reviewer.display_name='Independent updated reviewer';
render(next,managed);
out.heldSelection=selection.toString()===selected;out.heldContent=!body.textContent.includes('UPDATED');
selection.removeAllRanges();render(next,managed);
out.appliedAfterRelease=document.querySelector('.requirements-confirmation:not(.requirements-proposal) .requirements-body').textContent.includes('UPDATED');
// A real question card receives an updated proposal while the owner's answer is drafted.
const input=document.querySelector('[data-cq-other]'),fieldset=input.closest('[data-cq]');
fieldset.querySelector('input[value="other"]').checked=true;input.value='Please use my own greeting';input.focus();
const qtask=input.closest('[data-task]').dataset.task;
const changed=structuredClone(next);changed.state.requirement_proposals[qtask].proposed_at='2026-10-24T19:00:00Z';
render(changed,managed);out.focusedDraftKept=document.querySelector('[data-cq-other]')===input&&input.value==='Please use my own greeting';
input.blur();render(changed,managed);
out.draftAfterBlur=document.querySelector('[data-cq-other]').value;
out.chosenAfterBlur=document.querySelector('[data-cq-other]').closest('[data-cq]').querySelector('input[value="other"]').checked;
// Same saved dashboard before, during and after pause: content unchanged, controls must recover.
const paused=structuredClone(changed);paused.state.project_pause={status:'paused'};
render(paused,managed);out.copyWhilePaused=[...document.querySelectorAll('[data-copy-key]')].every(b=>!b.disabled);
out.disabledDuringPause=document.querySelector('.task-dynamic .stop.cancel-task').disabled;
render(changed,managed);
out.cancelEnabledAfterResume=!document.querySelector('.task-dynamic .stop.cancel-task').disabled;
out.proposalEnabledAfterResume=!document.querySelector('[data-req-go]').disabled;
// Copy the proposal, directive and historical agreement using the production event handler.
Object.defineProperty(navigator,'clipboard',{value:{writeText:async t=>window.copied=t},configurable:true});
for(const [key,selector] of [['proposal','.requirements-proposal [data-copy-key]'],['directive','.directive [data-copy-key]']]){
const button=document.querySelector(selector);window.copied=null;button.click();await wait(100);out[key+'Copy']={copied:window.copied,expected:copyTexts.get(button.dataset.copyKey),label:button.textContent};}
renderHistory([{task:'COMPLETED',completed_at:'2026-10-24T19:00:00Z',owner_direction:'Raw **directive**\n two spaces',requirements_confirmation:{text:'Raw *agreement*\n  indentation',version:1}}],'');
const history=document.querySelector('.history-requirements [data-copy-key]');window.copied=null;history.click();await wait(100);out.historyCopy=window.copied;
}catch(e){out.error=String(e);}
await fetch('/__probe__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(out)});
})();</script>'''
class Challenge(unittest.TestCase):
 def test_rendered_normal_interactions(self):
  with tempfile.TemporaryDirectory() as tmp:
   fixture=stability.RenderedPageStabilityTests();root,_,_=fixture.fixture(Path(tmp))
   result=run_page(self,root,PROBE,width=1100,height=600,wait=60)
   print('REVIEWER_CHALLENGE '+json.dumps(result,sort_keys=True),flush=True)
  self.assertNotIn('error',result)
  failures=[]
  for key in ['heldSelection','heldContent','appliedAfterRelease','focusedDraftKept','copyWhilePaused','disabledDuringPause','cancelEnabledAfterResume','proposalEnabledAfterResume']:
   if not result.get(key):failures.append(key)
  if result.get('draftAfterBlur')!='Please use my own greeting':failures.append('draftAfterBlur')
  if not result.get('chosenAfterBlur'):failures.append('chosenAfterBlur')
  for key in ['proposalCopy','directiveCopy']:
   item=result.get(key,{})
   if item.get('copied')!=item.get('expected') or item.get('label')!='Copied':failures.append(key)
  if result.get('historyCopy')!='Raw *agreement*\n  indentation':failures.append('historyCopy')
  self.assertEqual(failures,[])
if __name__=='__main__':unittest.main(verbosity=2)

# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Viewer refresh restructure — prove the Delivery Progress window stops jumping.

A task card is now split into two independently-refreshed regions:
  * .task-static  — the settled agreement: title, the full user directive, the
                    final agreed requirements. Written once; rewritten ONLY when it
                    genuinely changes.
  * .task-dynamic — the "Live delivery status" sub-window: progress, the plain-
                    language update, the next step. The only region that moves on
                    the two-second tick.

The load-bearing simulation is test_static_region_never_rebuilt_when_only_progress_moves:
it drives the REAL tasks() render through the actual page JS (in Node, via a fake DOM
that persists nodes across refreshes AND counts every innerHTML write per region) and
proves that when only the live progress changes, the static directive region is written
ZERO additional times — the long directive you are reading is never rebuilt underneath
you — while the live sub-window updates in place with exactly one write. An unchanged
tick writes nothing at all.

Counting writes per region is the rigorous proof: a snapshot string comparison only
shows content is equal, not that the DOM was left alone.

Run:  PYTHONPATH=. python3 -m unittest tests.test_viewer_refresh -v
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import board, board_viewer, contract, control


def _page_declarations() -> str:
    """The page's JS function declarations, without the DOM-wiring tail."""
    script = board_viewer.rendered_page().split("<script>", 1)[1].split("</script>", 1)[0]
    return script.split("el('#status-dialog-close')", 1)[0]


# A fake DOM faithful to what the real refresh needs: a #tasks container whose
# children persist across successive tasks() calls and that answers
# querySelectorAll('.task'); card nodes that hold child region containers
# (appendChild + querySelector) and whose innerHTML getter concatenates them; and
# region containers whose innerHTML SETTER counts writes, so the test can observe
# exactly which region was rewritten (or left untouched) on each refresh.
_FAKE_DOM = r"""
const out={
  children:[], _html:'', scrollTop:0, scrollHeight:0, clientHeight:0,
  get innerHTML(){return this._html;}, set innerHTML(v){this._html=v;},
  replaceChildren(){this.children=[];},
  append(node){const i=this.children.indexOf(node); if(i>=0)this.children.splice(i,1); this.children.push(node);},
  querySelectorAll(sel){const cls=sel.replace(/^\./,''); return this.children.filter(c=>String(c.className||'').split(' ').includes(cls));}
};
function makeNode(tag){
  const node={tagName:tag,className:'',dataset:{},_kids:[],_html:'',_writes:0,
    appendChild(child){node._kids.push(child); return child;},
    querySelector(sel){const cls=sel.replace(/^\./,''); return node._kids.find(k=>String(k.className||'').split(' ').includes(cls))||null;},
    remove(){const i=out.children.indexOf(node); if(i>=0)out.children.splice(i,1);}
  };
  Object.defineProperty(node,'innerHTML',{
    get(){return node._kids.length?node._kids.map(k=>k.innerHTML).join(''):node._html;},
    set(v){node._html=v; node._kids=[]; node._writes++;}   // a write to THIS node's own content
  });
  return node;
}
globalThis.window={innerHeight:900,addEventListener(){}};
globalThis.document={querySelector(sel){return sel==='#tasks'?out:null;},createElement(tag){return makeNode(tag);}};
"""


class ViewerRefresh(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        session = control.create(root, "codex_delivery")
        agent = board.register(root, "development", board.AWAITING_OWNER_DIRECTION,
                               vendor="OpenAI", session_id=session["id"])
        board.record_owner_direction(root, session["id"], "Full directive text for the refresh test.")
        board.begin_task(root, agent["id"], "VIEW-JUMP")
        contract.create_contract(root, "VIEW-JUMP", "Full directive text for the refresh test.", ["render"])
        board.task_brief(root, agent["id"], "The delivery plan.", "Update ONE")
        dash = board_viewer.dashboard_payload(root)
        self.state = json.dumps(dash["state"])
        self.contracts = json.dumps(dash["contracts"])
        self.directions = json.dumps(dash["owner_directions"])
        self.requirements = json.dumps(dash.get("requirement_confirmations", {}))
        self.task = "VIEW-JUMP"

    def _run(self, body: str) -> dict:
        script = _page_declarations() + "\n" + _FAKE_DOM + "\n" + body
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_title_is_first_in_card(self):
        out = self._run(f"""
        tasks({self.state},{self.contracts},{self.directions},['{self.task}'],[],{self.requirements});
        const html=out.children[0].innerHTML;
        process.stdout.write(JSON.stringify({{
          headPos:html.indexOf('task-head'),
          directivePos:html.indexOf('class="directive"'),
          livePos:html.indexOf('class="live-label"'),
          countsPos:html.indexOf('class="task-counts"')
        }}));
        """)
        # Title (task-head) leads, directive below it, then the live sub-window.
        self.assertGreaterEqual(out["headPos"], 0)
        self.assertLess(out["headPos"], out["directivePos"])
        self.assertLess(out["directivePos"], out["livePos"])
        self.assertLess(out["livePos"], out["countsPos"])

    def test_unchanged_tick_touches_nothing(self):
        out = self._run(f"""
        const state={self.state}, contracts={self.contracts}, directions={self.directions}, requirements={self.requirements};
        tasks(state,contracts,directions,['{self.task}'],[],requirements);
        const card=out.children[0];
        card.__probe='ALIVE';
        const s=card.querySelector('.task-static'), d=card.querySelector('.task-dynamic');
        const sBuilt=s._writes, dBuilt=d._writes;
        // A normal 2-second tick with nothing changed.
        tasks(state,contracts,directions,['{self.task}'],[],requirements);
        process.stdout.write(JSON.stringify({{
          reusedCard: out.children[0]===card,
          probeSurvived: out.children[0].__probe==='ALIVE',
          staticBuilt:sBuilt, staticAfter:s._writes,
          dynamicBuilt:dBuilt, dynamicAfter:d._writes
        }}));
        """)
        self.assertTrue(out["reusedCard"], "card node recreated on an unchanged refresh")
        self.assertTrue(out["probeSurvived"], "sentinel lost — the card was rebuilt")
        # Each region is built exactly once...
        self.assertEqual(out["staticBuilt"], 1)
        self.assertEqual(out["dynamicBuilt"], 1)
        # ...and an unchanged tick rewrites NEITHER region. Nothing moves, nothing jumps.
        self.assertEqual(out["staticAfter"], 1, "unchanged tick rewrote the static region")
        self.assertEqual(out["dynamicAfter"], 1, "unchanged tick rewrote the live sub-window")

    def test_static_region_never_rebuilt_when_only_progress_moves(self):
        # THE load-bearing proof of the user's request: when only the live status
        # changes, the settled directive/requirements region is never touched.
        out = self._run(f"""
        const state={self.state}, contracts={self.contracts}, directions={self.directions}, requirements={self.requirements};
        tasks(state,contracts,directions,['{self.task}'],[],requirements);
        const card=out.children[0];
        const s=card.querySelector('.task-static'), d=card.querySelector('.task-dynamic');
        s.__probe='STATIC-ALIVE';                 // survives only if the static node is reused
        const sBuilt=s._writes, dBuilt=d._writes;

        // The plain-language Delivery update changes — a live-status change only.
        const changed=JSON.parse(JSON.stringify(state));
        changed.task_briefs['{self.task}']={{plan:'The delivery plan.',update:'Update TWO changed'}};
        tasks(changed,contracts,directions,['{self.task}'],[],requirements);

        const sameCard=out.children[0]===card;
        const sameStatic=card.querySelector('.task-static')===s;
        const liveHtml=d.innerHTML;
        process.stdout.write(JSON.stringify({{
          sameCard, sameStatic,
          staticProbeSurvived: s.__probe==='STATIC-ALIVE',
          staticBuilt:sBuilt, staticAfter:s._writes,     // must NOT grow
          dynamicBuilt:dBuilt, dynamicAfter:d._writes,   // exactly one more
          liveShowsChange: liveHtml.includes('Update TWO changed')
        }}));
        """)
        self.assertTrue(out["sameCard"], "a live-status change recreated the whole card")
        self.assertTrue(out["sameStatic"], "the static region node was recreated")
        self.assertTrue(out["staticProbeSurvived"], "static region sentinel lost — it was rebuilt")
        # The static directive/requirements region is NEVER rewritten on a progress move.
        self.assertEqual(out["staticBuilt"], 1)
        self.assertEqual(out["staticAfter"], 1,
                         "the settled directive region was rewritten by a mere progress change")
        # Only the live sub-window updates, in place, exactly once.
        self.assertEqual(out["dynamicBuilt"], 1)
        self.assertEqual(out["dynamicAfter"], 2, "the live sub-window should update exactly once")
        self.assertTrue(out["liveShowsChange"], "the live sub-window did not reflect the change")

    def test_gone_task_card_is_removed(self):
        out = self._run(f"""
        const state={self.state}, contracts={self.contracts}, directions={self.directions}, requirements={self.requirements};
        tasks(state,contracts,directions,['{self.task}'],[],requirements);
        const had=out.children.length;
        // Next refresh: the task is no longer live → its card must be removed.
        tasks(state,contracts,directions,[],[],requirements);
        process.stdout.write(JSON.stringify({{had,after:out.children.length}}));
        """)
        self.assertEqual(out["had"], 1)
        self.assertEqual(out["after"], 0)


if __name__ == "__main__":
    unittest.main()

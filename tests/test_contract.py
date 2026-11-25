# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
import json, tempfile, unittest
from pathlib import Path
from harness import contract

class ContractTests(unittest.TestCase):
 def test_contract_immutable_definition_survives_evidence_and_append_only_expansion(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); contract.create_contract(root,"IMMUTABLE","Ship the governed feature",["first","second"])
   path=root/".harness"/"tasks"/"IMMUTABLE.json"
   creation=json.loads(path.read_text())
   tampered=json.loads(json.dumps(creation)); tampered["objective"]="silently replaced before expansion"
   path.write_text(json.dumps(tampered))
   self.assertFalse(contract.contract_preflight(root,"IMMUTABLE")[0])
   # Restore the exact creation snapshot rather than allowing the tampered
   # definition to become a new baseline.
   path.write_text(json.dumps(creation))
   evidence=root/"evidence.txt"; evidence.write_text("first proof\n")
   contract.add_evidence(root,"IMMUTABLE","first",[evidence])
   contract.expand_contract(root,"IMMUTABLE",[("necessary recovery","Executable recovery simulation passes")])
   contract.add_evidence(root,"IMMUTABLE","second",[evidence])
   contract.add_evidence(root,"IMMUTABLE","necessary recovery",[evidence])
   self.assertTrue(contract.contract_complete(root,"IMMUTABLE")[0])
   stable=json.loads(path.read_text())
   mutations=[]
   altered=json.loads(json.dumps(stable)); altered["objective"]="changed objective"; mutations.append(altered)
   altered=json.loads(json.dumps(stable)); altered["deliverables"][0]["name"]="renamed original"; mutations.append(altered)
   altered=json.loads(json.dumps(stable)); altered["deliverables"][0]["acceptance_proof"]="changed proof"; mutations.append(altered)
   altered=json.loads(json.dumps(stable)); altered["deliverables"].pop(0); mutations.append(altered)
   altered=json.loads(json.dumps(stable)); altered["deliverables"]=list(reversed(altered["deliverables"])); mutations.append(altered)
   for altered in mutations:
    path.write_text(json.dumps(altered))
    self.assertFalse(contract.contract_preflight(root,"IMMUTABLE")[0])
   path.write_text(json.dumps(stable))
   self.assertTrue(contract.contract_preflight(root,"IMMUTABLE")[0])
 def test_contract_evidence_and_claim_lint(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); contract.create_contract(root,"T1","Ship feature",["API","UI"])
   evidence=root/"e.txt"; evidence.write_text("proof")
   contract.add_evidence(root,"T1","API",[evidence])
   bad=contract.lint_handoff(root,"T1","OBJECTIVE STATUS: COMPLETE\nCompleted: API\nRemaining: UI\nEvidence: e")
   self.assertFalse(bad["valid"])
   contract.add_evidence(root,"T1","UI",[evidence])
   good=contract.lint_handoff(root,"T1","OBJECTIVE STATUS: COMPLETE\nCompleted: API, UI\nRemaining: none\nEvidence: e")
   self.assertTrue(good["valid"])
 def test_partial_completion_word_is_blocked(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); contract.create_contract(root,"T2","Ship feature",["API"])
   got=contract.lint_handoff(root,"T2","OBJECTIVE STATUS: PARTIAL\nCompleted: API\nRemaining: QA\nEvidence: none\nThis is done")
   self.assertFalse(got["valid"])
 def test_owner_direction_normalization_removes_terminal_protocol_replies(self):
  noise="\x1b[7;85R\x1b]10;rgb:e6ce/e6ce/e6ce\x07\x1b]11;rgb:05b1/06cf/0923\x07\x1b[?1;2c\x1b[O\x1b[I"
  direction="Run every Scenario Ledger simulation and record its actual result"
  self.assertEqual(contract.normalize_owner_direction(noise + direction), direction)
 def test_owner_direction_normalization_removes_trailing_dcs_and_c1_replies(self):
  direction="Run every scenario, including owner prose about OWNER DIRECTION"
  noise="\x1bP1$r0m\x1b\\\x9b7;85R\x90tmux;passthrough\x9c"
  self.assertEqual(contract.normalize_owner_direction(direction + noise), direction)
 def test_executable_scenario_plan_is_accepted(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/"ledger.md"
   path.write_text("| ID | Scenario | Simulation command | Expected system response | Observed system response | QA result |\n|---|---|---|---|---|---|\n| S-001 | dependency fails | `python3 -m unittest test_retry` | Retry is bounded and state is preserved | PASS: targeted retry test ran and asserted one retry with preserved state | PASS |\n")
   valid, problems, rows = contract.scenario_simulations(path)
   self.assertTrue(valid, problems)
   self.assertEqual(rows[0]["command"], "python3 -m unittest test_retry")
   self.assertTrue(contract.scenario_ledger_complete(path)[0])

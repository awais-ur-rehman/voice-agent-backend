import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from evaluation.flows import load_probes, build_probe_payload, main
from evaluation.flow_scoring import score_action, score_language
from evaluation.review import render_report


def response(name='find_appointments', arguments='{"patient_id":"patient_fixture"}', content=''):
    return {'tools':[{'name':name,'arguments':arguments}], 'content':content, 'reasoning_chars':0}


class ActionScoringTests(unittest.TestCase):
    expected = {'name':'find_appointments','arguments':{'patient_id':'patient_fixture'}}

    def test_wrong_subject_and_added_tenant_argument_fail(self):
        self.assertTrue(score_action(self.expected,response())['passed'])
        self.assertFalse(score_action(self.expected,response(arguments='{"patient_id":"other"}'))['passed'])
        self.assertFalse(score_action(self.expected,response(arguments='{"patient_id":"patient_fixture","tenant_id":"other"}'))['passed'])

    def test_devanagari_in_tool_arguments_and_duplicate_keys_fail(self):
        expected={'name':'update_dialogue_state','arguments':{'date_phrase':'अगले منگل'}}
        result=score_action(expected,response('update_dialogue_state','{"date_phrase":"अगले منگل"}'))
        self.assertFalse(result['script_ok'])
        self.assertFalse(result['passed'])
        self.assertFalse(score_action(self.expected,response(arguments='{"patient_id":"other","patient_id":"patient_fixture"}'))['passed'])

    def test_valid_call_cannot_hide_reasoning_or_free_prose(self):
        r=response(); r['reasoning_chars']=2
        self.assertFalse(score_action(self.expected,r)['passed'])
        self.assertFalse(score_action(self.expected,response(content='Cancelled already'))['passed'])

    def test_numeric_one_cannot_impersonate_json_boolean_true(self):
        expected={'name':'update_dialogue_state','arguments':{'patch':{'task':'cancel','park_current':True}}}
        wrong=response('update_dialogue_state','{"patch":{"task":"cancel","park_current":1}}')
        self.assertFalse(score_action(expected,wrong)['passed'])


class LanguageScoringTests(unittest.TestCase):
    def test_script_and_digits_and_extra_questions_are_rejected(self):
        case={'expected_lang':'ur'}
        for text in ['Ji appointment chahiye?', 'नमस्ते', 'جی، وقت 10 بجے ہے۔', 'کب؟ کہاں؟']:
            with self.subTest(text=text):
                self.assertFalse(score_language(case,{'content':text,'tools':[]})['passed'])
        self.assertTrue(score_language(case,{'content':'جی، آپ کس وقت آنا چاہیں گے؟','tools':[]})['passed'])

    def test_code_say_cannot_be_rephrased(self):
        case={'expected_lang':'en','say':'The fee is two thousand rupees.'}
        self.assertTrue(score_language(case,{'content':case['say'],'tools':[]})['passed'])
        self.assertFalse(score_language(case,{'content':'The fee is three thousand rupees.','tools':[]})['passed'])


class ProbeTests(unittest.TestCase):
    def test_loader_pairs_thirty_scenarios_and_has_human_review_samples(self):
        probes=load_probes()
        actions=[p for p in probes if p['suite']=='actions']
        self.assertEqual(len(actions),60)
        self.assertEqual(len({p['scenario_id'] for p in actions}),30)
        self.assertEqual({p['lang'] for p in actions},{'en','ur'})
        self.assertEqual(len([p for p in probes if p['suite']=='language']),12)

    def test_payload_keeps_thinking_off_and_does_not_expose_expected_answer(self):
        probe=load_probes()[0]
        payload=build_probe_payload('nvidia/nemotron-3.5-bolt-e4b',probe)
        self.assertEqual(payload['chat_template_kwargs'],{'enable_thinking':False})
        self.assertEqual(payload['response_format']['type'],'json_schema')
        native=build_probe_payload('nvidia/nemotron-3.5-bolt-e4b',probe,action_protocol='native')
        self.assertEqual(native['tool_choice'],'required')
        self.assertIn('fixture_only',payload['messages'][0]['content'])
        self.assertNotIn('expected',payload['messages'][0]['content'])
        prose=next(p for p in load_probes() if p['suite']=='language')
        self.assertNotIn('tools',build_probe_payload('nvidia/nemotron-3.5-bolt-e4b',prose))

    def test_dry_run_is_offline(self):
        from contextlib import redirect_stdout
        output=io.StringIO()
        with redirect_stdout(output), patch('evaluation.flows.request_stream',side_effect=AssertionError('network forbidden')):
            self.assertEqual(main(['--dry-run']),0)
        self.assertEqual(json.loads(output.getvalue())['probes'],72)

    def test_one_scenario_can_be_retested_in_both_languages_offline(self):
        from contextlib import redirect_stdout
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--dry-run','--scenario','correct-date']),0)
        self.assertEqual(json.loads(output.getvalue())['probes'],2)

    def test_html_escapes_model_output_and_marks_human_review_pending(self):
        document=render_report({'model':'test','cases':[{'id':'x','input':'<script>bad()</script>',
             'status':'ok','scores':{'passed':False},'response':{'content':'<img src=x onerror=bad()>','tools':[]}}]})
        self.assertNotIn('<script>bad()',document)
        self.assertIn('&lt;script&gt;',document)
        self.assertIn('Human review pending',document)
        self.assertIn('dir="auto"',document)


if __name__=='__main__':unittest.main()

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation.action_protocol import action_schema, decode_action_response
from evaluation.delivery import confirmation_text
from evaluation.flows import ROOT, build_probe_payload, evaluate_probe, load_probes, main


def response(content, **extra):
    return {'content':content,'tools':[],'reasoning_chars':0,'usage':None,
            'first_output_ms':100,'first_content_ms':100,'first_usable_content_ms':100,'total_ms':120,**extra}


class ActionProtocolTests(unittest.TestCase):
    def test_structured_mode_keeps_all_tools_without_expected_answer(self):
        probe=load_probes()[0]
        payload=build_probe_payload('nvidia/nemotron-3.5-bolt-e4b',probe)
        self.assertNotIn('tools',payload)
        self.assertNotIn('tool_choice',payload)
        self.assertEqual(payload['response_format']['type'],'json_schema')
        self.assertEqual(len(payload['response_format']['json_schema']['schema']['anyOf']),13)
        self.assertNotIn('expected',payload['messages'][0]['content'])
        self.assertIn('ask_caller',json.dumps(action_schema()))

    def test_whole_valid_json_action_is_decoded_and_raw_output_preserved(self):
        raw='{"name":"ask_caller","arguments":{"field":"confirmation"}}'
        decoded=decode_action_response(response(raw))
        self.assertEqual(decoded['tools'],[{'name':'ask_caller','arguments':'{"field":"confirmation"}'}])
        self.assertEqual(decoded['content'],'')
        self.assertEqual(decoded['raw_content'],raw)
        self.assertEqual(decoded['action_protocol'],'json_schema')

    def test_function_text_unknown_tool_duplicate_keys_and_extra_arguments_fail_closed(self):
        invalid=['ask_caller{field:<|"|>time<|"|>}',
                 '{"name":"invented","arguments":{}}',
                 '{"name":"ask_caller","arguments":{"field":"time","tenant_id":"other"}}',
                 '{"name":"ask_caller","name":"hold_slot","arguments":{"slot_id":"x"}}',
                 'prefix {"name":"ask_caller","arguments":{"field":"time"}}',
                 '{"name":"update_dialogue_state","arguments":{"patch":{"park_current":1}}}',
                 '{"name":"update_dialogue_state","arguments":{"date_phrase":"अगले منگل"}}']
        for raw in invalid:
            with self.subTest(raw=raw),self.assertRaises(ValueError):decode_action_response(response(raw))

    def test_reasoning_and_mixed_native_protocol_are_rejected(self):
        raw='{"name":"ask_caller","arguments":{"field":"time"}}'
        for r in [response(raw,reasoning_chars=1),response(raw,tools=[{'name':'ask_caller','arguments':'{}'}])]:
            with self.assertRaises(ValueError):decode_action_response(r)

    def test_unicode_escaped_devanagari_is_checked_after_json_decoding(self):
        raw=json.dumps({'name':'update_dialogue_state','arguments':{'date_phrase':'अगले'}})
        with self.assertRaises(ValueError):decode_action_response(response(raw))


class ConfirmationTests(unittest.TestCase):
    envelope={'ok':True,'data':{'fixture_only':True},'say':{'en':'The fee is two thousand rupees.','ur':'فیس دو ہزار روپے ہے۔'},'error':None}

    def test_confirmation_is_exact_and_never_appended_or_rephrased(self):
        self.assertEqual(confirmation_text(self.envelope,'ur'),'فیس دو ہزار روپے ہے۔')
        self.assertEqual(confirmation_text(self.envelope,'en'),'The fee is two thousand rupees.')
        with self.assertRaises(ValueError):confirmation_text(self.envelope,'unknown')

    def test_invalid_envelope_and_blank_say_fail_closed(self):
        for r in [dict(self.envelope,error={'code':'not_owner','retryable':False}),
                  dict(self.envelope,say={'en':'','ur':'فیس دو ہزار روپے ہے۔'})]:
            with self.assertRaises(ValueError):confirmation_text(r,'en')

    def test_authoritative_confirmation_bypasses_network_completely(self):
        probe=next(p for p in load_probes() if p['id']=='say-date-ur')
        with patch('evaluation.flows.request_stream',side_effect=AssertionError('network must not be called')):
            row=evaluate_probe('nvidia/nemotron-3.5-bolt-e4b',probe,'unused','https://example.com/v1',30)
        self.assertEqual(row['response']['content'],probe['say'])
        self.assertEqual(row['delivery_source'],'code_template')
        self.assertTrue(row['scores']['passed'])
        self.assertIsNone(row['response']['first_output_ms'])

    def test_authoritative_confirmation_cannot_be_built_as_a_model_request(self):
        probe=next(p for p in load_probes() if p['id']=='say-date-ur')
        with self.assertRaises(ValueError):build_probe_payload('nvidia/nemotron-3.5-bolt-e4b',probe)

    def test_confirmation_only_cli_needs_no_key_and_has_no_model_latency_samples(self):
        import shutil
        folder=Path(tempfile.mkdtemp(dir=ROOT/'results'));self.addCleanup(shutil.rmtree,folder)
        output=folder/'code-only.json'
        with patch('evaluation.flows.load_env',side_effect=AssertionError('No credentials needed')), \
             patch('evaluation.flows.request_stream',side_effect=AssertionError('No model call allowed')), \
             redirect_stdout(io.StringIO()):
            code=main(['--suite','confirmations','--output',str(output)])
        self.assertEqual(code,0)
        report=json.loads(output.read_text())
        self.assertIsNone(report['model'])
        self.assertEqual(report['summary']['model_responses'],0)
        self.assertEqual(report['summary']['code_deliveries'],3)
        self.assertEqual(report['summary']['language']['latency_samples'],0)

    def test_invalid_late_confirmation_fixture_is_rejected_before_any_model_request(self):
        fixture=ROOT/'evaluation/fixtures/language.json'
        cases=json.loads(fixture.read_text())
        next(c for c in cases if c['id']=='say-date-ur')['tool_result']['say']['en']=''
        original=Path.read_text
        def read(path,*args,**kwargs):
            return json.dumps(cases) if path==fixture else original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read), \
             patch('evaluation.flows.load_env',side_effect=AssertionError('Preflight must reject before credential loading')), \
             patch('evaluation.flows.request_stream',side_effect=AssertionError('Preflight must reject before inference')), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--suite','language']),2)

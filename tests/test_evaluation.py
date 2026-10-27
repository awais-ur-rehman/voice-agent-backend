import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.scoring import percentile, score
from evaluation.stream import read_stream
from evaluation.cli import load_env, validate_endpoint, request_stream, main, build_payload


def event(delta, finish=None):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta,
            'finish_reason': finish}]}) + '\n\n').encode()


class StreamTests(unittest.TestCase):
    def test_malformed_content_and_indices_raise_value_error(self):
        for delta in [{'content': ['unexpected']}, {'content': {'text': 'unexpected'}},
                      {'tool_calls': [{'index': -1, 'function': {'name': 'x'}}]},
                      {'tool_calls': [{'index': True, 'function': {'name': 'x'}}]}]:
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                read_stream(io.BytesIO(event(delta) + event({}, 'stop') + b'data: [DONE]\n\n'), 0, lambda: 1)

    def test_fragmented_think_tags_do_not_claim_usable_content_timing(self):
        data = event({'content': '<thi'}) + event({'content': 'nk>reason</think>جی'})
        data += event({}, 'stop') + b'data: [DONE]\n\n'
        result = read_stream(io.BytesIO(data), 0, lambda: 1)
        self.assertTrue(result['reasoning_leak'])
        self.assertIsNone(result['first_usable_content_ms'])
        self.assertEqual(result['first_content_ms'], 1000)

    def test_merges_tool_fragments_and_ignores_role_only_delta(self):
        data = b''.join([
            event({'role': 'assistant'}),
            event({'tool_calls': [{'index': 0, 'id': 'call_1', 'type': 'function',
                   'function': {'name': 'get_clinic_info', 'arguments': '{"field":'}}]}),
            event({'tool_calls': [{'index': 0, 'function': {'arguments': '"hours"}'}}]}),
            event({}, 'tool_calls'), b'data: [DONE]\n\n'])
        clock = iter([1.0, 1.1, 1.2, 1.3, 1.4])
        result = read_stream(io.BytesIO(data), 1.0, lambda: next(clock))
        self.assertEqual(result['tools'], [{'name': 'get_clinic_info', 'arguments': '{"field":"hours"}'}])
        self.assertAlmostEqual(result['first_output_ms'], 100)
        self.assertIsNone(result['first_content_ms'])

    def test_content_timing_excludes_reasoning_and_metadata(self):
        data = event({'role': 'assistant'}) + event({'reasoning_content': 'hidden'})
        data += event({'content': 'جی'}) + event({}, 'stop') + b'data: [DONE]\n\n'
        result = read_stream(io.BytesIO(data), 0, iter([0.1, 0.2, 0.3, 0.4, 0.5]).__next__)
        self.assertEqual(result['content'], 'جی')
        self.assertAlmostEqual(result['first_content_ms'], 300)
        self.assertEqual(result['reasoning_chars'], 6)

    def test_incomplete_or_error_stream_is_not_successful(self):
        for data in [event({'content': 'partial'}), b'data: {"error":{"message":"bad"}}\n\n',
                     b'data: not-json\n\n', b'data: [DONE]\n\n']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                read_stream(io.BytesIO(data), 0, lambda: 1)


class ScoringTests(unittest.TestCase):
    case = {'expected': {'name': 'get_clinic_info', 'arguments': {'field': 'hours'}}, 'lang': 'ur'}

    def response(self, args, name='get_clinic_info', content=''):
        return {'tools': [{'name': name, 'arguments': args}], 'content': content}

    def test_schema_validity_does_not_imply_correct_arguments(self):
        result = score(self.case, self.response('{"field":"address"}'))
        self.assertTrue(result['schema_valid'])
        self.assertFalse(result['arguments_match'])
        self.assertFalse(result['passed'])

    def test_inline_reasoning_fails_even_when_tool_arguments_are_correct(self):
        case = dict(self.case, lang='en')
        result = score(case, self.response('{"field":"hours"}', content='<think>reasoning</think>'))
        self.assertTrue(result['reasoning_leak'])
        self.assertFalse(result['passed'])

    def test_separate_reasoning_is_reported_even_when_tools_are_correct(self):
        response = self.response('{"field":"hours"}')
        response['reasoning_chars'] = 6
        result = score(self.case, response)
        self.assertTrue(result['reasoning_returned'])
        self.assertFalse(result['passed'])

    def test_mixed_input_does_not_assume_urdu_response(self):
        case = dict(self.case, lang='mixed')
        result = score(case, self.response('{"field":"hours"}', content='Let me check.'))
        self.assertIsNone(result['urdu_script_ok'])
        self.assertTrue(result['passed'])

    def test_invalid_json_unknown_fields_and_tool_names_fail(self):
        for args, name in [('no', 'get_clinic_info'), ('{"field":"hours","extra":1}', 'get_clinic_info'),
                           ('{"field":"hours"}', 'invented'), ('[]', 'get_clinic_info'),
                           ('{"field":"hours","field":"address"}', 'get_clinic_info')]:
            with self.subTest(args=args, name=name):
                self.assertFalse(score(self.case, self.response(args, name))['schema_valid'])

    def test_urdu_script_check_is_not_assumed_for_tool_only_response(self):
        self.assertIsNone(score(self.case, self.response('{"field":"hours"}'))['urdu_script_ok'])
        self.assertFalse(score(self.case, self.response('{"field":"hours"}', content='नमस्ते'))['urdu_script_ok'])
        self.assertTrue(score(self.case, self.response('{"field":"hours"}', content='جی بتائیں'))['urdu_script_ok'])

    def test_nearest_rank_percentiles_handle_empty_and_small_samples(self):
        self.assertIsNone(percentile([], 95))
        self.assertEqual(percentile([10, 30, 20, 40], 50), 20)
        self.assertEqual(percentile([10, 30, 20, 40], 95), 40)


class ConfigurationTests(unittest.TestCase):
    def test_comparison_models_use_documented_individual_settings(self):
        minimax = build_payload('minimaxai/minimax-m3.1-flash', 'Hello')
        self.assertEqual(minimax['temperature'], 1.0)
        self.assertEqual(minimax['top_p'], 0.95)
        self.assertNotIn('chat_template_kwargs', minimax)
        muse = build_payload('meta/muse-glimmer-30b', 'Hello')
        self.assertEqual(muse['reasoning_effort'], 'none')
        self.assertEqual(muse['temperature'], 0.95)
        self.assertEqual(muse['top_p'], 1.0)
        diffusion = build_payload('google/diffusiongemma-26b-a4b-it', 'Hello')
        self.assertEqual(diffusion['chat_template_kwargs'], {'enable_thinking': False})
        self.assertEqual(diffusion['max_tokens'], 300)

    def test_nemotron_request_explicitly_disables_thinking(self):
        payload = build_payload('nvidia/nemotron-3.5-bolt-e4b', 'کل وقت چاہیے۔')
        self.assertEqual(payload['chat_template_kwargs'], {'enable_thinking': False})
        self.assertIs(payload['parallel_tool_calls'], False)
        self.assertIs(payload['skip_special_tokens'], False)
        self.assertEqual(payload['temperature'], 1.0)
        self.assertEqual(payload['top_p'], 0.95)
        self.assertEqual(payload['messages'][-1]['content'], 'کل وقت چاہیے۔')
        self.assertNotIn('chat_template_kwargs', build_payload('another-model', 'Hello'))

    def test_report_counts_failed_scores_and_redacts_echoed_key(self):
        from contextlib import redirect_stdout, redirect_stderr
        import shutil
        from evaluation.cli import ROOT
        (ROOT / 'results').mkdir(exist_ok=True)
        directory = Path(tempfile.mkdtemp(dir=ROOT / 'results'))
        self.addCleanup(shutil.rmtree, directory)
        response = {'content': 'synthetic-secret', 'tools': [], 'first_output_ms': 100,
                    'first_content_ms': 100, 'first_usable_content_ms': 100,
                    'total_ms': 300, 'finish_reason': 'stop', 'usage': None}
        with patch.dict(os.environ, {'LLM_API_KEY': 'synthetic-secret'}, clear=True), \
                patch('evaluation.cli.load_env', return_value=None), \
                patch('evaluation.cli.request_stream', return_value=response), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(['--model', 'test', '--limit', '1', '--output', str(directory / 'report.json')])
        self.assertEqual(code, 1)
        raw = (directory / 'report.json').read_text()
        self.assertNotIn('synthetic-secret', raw)
        report = json.loads(raw)
        self.assertEqual(report['summary']['completed'], 1)
        self.assertEqual(report['summary']['passed'], 0)
        self.assertEqual(report['summary']['first_output_ms']['p95'], 100)

    def test_env_is_data_and_existing_environment_wins(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LLM_MODEL': 'existing'}, clear=True):
            path = Path(directory) / '.env'
            path.write_text('LLM_MODEL=replaced\nLLM_API_KEY="literal-$(command)"\n')
            load_env(path)
            self.assertEqual(os.environ['LLM_MODEL'], 'existing')
            self.assertEqual(os.environ['LLM_API_KEY'], 'literal-$(command)')

    def test_endpoint_rejects_remote_http_credentials_and_query(self):
        for url in ['http://example.com/v1', 'https://user:secret@example.com/v1',
                    'https://example.com/v1?key=secret', 'file:///tmp/test', 'https://example.com/v1#fragment']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_endpoint(url)
        self.assertEqual(validate_endpoint('http://127.0.0.1:8000/v1/'), 'http://127.0.0.1:8000/v1')

    def test_transport_does_not_leak_error_body_or_follow_redirect(self):
        from urllib.error import HTTPError
        for status in [401, 429, 302]:
            failure = HTTPError('https://example.com', status, 'secret-key', {}, io.BytesIO(b'secret-key'))
            with self.subTest(status=status), patch('evaluation.cli.OPENER.open', side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, f'HTTP {status}') as caught:
                    request_stream('https://example.com/v1', 'secret-key', {}, 3)
                self.assertNotIn('secret-key', str(caught.exception))

    def test_timeout_and_invalid_stream_are_distinct_without_secret_details(self):
        for failure, message in [(TimeoutError('secret-key'), 'Provider timed out'),
                                 (ValueError('secret-key'), 'Provider returned an invalid or incomplete stream')]:
            with self.subTest(failure=type(failure).__name__), \
                    patch('evaluation.cli.OPENER.open', side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, message) as caught:
                    request_stream('https://example.com/v1', 'secret-key', {}, 3)
                self.assertNotIn('secret-key', str(caught.exception))

    def test_cli_dry_run_and_missing_key(self):
        from contextlib import redirect_stderr
        command = [sys.executable, '-m', 'evaluation']
        env = dict(os.environ, LLM_API_KEY='', LLM_MODEL='')
        dry = subprocess.run(command + ['--dry-run'], capture_output=True, text=True, env=env)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)['languages'], {'ur': 20, 'en': 10, 'mixed': 10})
        error = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch('evaluation.cli.load_env', return_value=None), redirect_stderr(error):
            self.assertEqual(main(['--model', 'test']), 2)
        self.assertIn('LLM_API_KEY', error.getvalue())


if __name__ == '__main__':
    unittest.main()

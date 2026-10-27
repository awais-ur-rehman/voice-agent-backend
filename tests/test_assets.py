import copy
import json
import unittest
from evaluation.assets import validate_assets, validate_envelope


class AssetTests(unittest.TestCase):
    def test_all_golden_ranges_and_recording_slots_are_present(self):
        result=validate_assets()
        self.assertEqual(result['number_values'],101)
        self.assertGreaterEqual(result['resolver_cases'],30)
        self.assertEqual(result['recording_prompts'],{'ur':200,'mixed':50})
        self.assertEqual(result['speakers'],10)
        self.assertEqual(result['recorded_audio'],0)

    def test_tool_envelope_forbids_success_error_and_failure_patient_data(self):
        say={'en':'Not found','ur':'نہیں ملا'}
        good={'ok':False,'data':None,'say':say,'error':{'code':'not_owner','retryable':False}}
        self.assertTrue(validate_envelope(good))
        bad=copy.deepcopy(good);bad['data']={'patient':'other'}
        self.assertFalse(validate_envelope(bad))
        bad=copy.deepcopy(good);bad['ok']=True
        self.assertFalse(validate_envelope(bad))

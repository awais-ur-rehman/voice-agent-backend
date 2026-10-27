"""State/input eligibility is independent of evaluation expectations."""

import copy
from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from evaluation.eligibility import state_action_schema
from evaluation.action_protocol import decode_action_response
from evaluation.flows import build_probe_payload, load_probes, main


BASE = {'state_version': 1, 'fixture_only': True, 'task': 'book', 'stage': 'collect_need'}


def allowed(state, name, arguments, text='I want next Saturday.'):
    return Draft202012Validator(state_action_schema(state, text)).is_valid({'name': name, 'arguments': arguments})


class EligibilityTests(unittest.TestCase):
    def test_raw_date_is_a_literal_span_and_cannot_be_translated_or_copied_from_examples(self):
        self.assertTrue(allowed(BASE, 'update_dialogue_state', {'date_phrase': 'next Saturday'}))
        for phrase in ['اگلے ہفتے', '2027-03-16', 'tomorrow', '']:
            self.assertFalse(allowed(BASE, 'update_dialogue_state', {'date_phrase': phrase}))
        self.assertTrue(allowed(BASE, 'update_dialogue_state', {'date_phrase': 'پرسوں'}, 'پرسوں وقت چاہیے۔'))

    def test_questions_are_eligible_only_for_the_pending_stage(self):
        for field in ['time', 'appointment', 'confirmation', 'verification_factor']:
            self.assertFalse(allowed(BASE, 'ask_caller', {'field': field}))
        resumed = BASE | {'date': '2028-02-19', 'awaiting': 'time'}
        self.assertTrue(allowed(resumed, 'ask_caller', {'field': 'time'}))
        self.assertFalse(allowed(resumed, 'ask_caller', {'field': 'confirmation'}))
        confirm = BASE | {'stage': 'confirm', 'critical_fields_confirmed': False}
        self.assertTrue(allowed(confirm, 'ask_caller', {'field': 'confirmation'}))

    def test_verification_threshold_and_private_lookup_gate(self):
        state = BASE | {'task': 'cancel', 'stage': 'verify', 'verification': {'status': 'failed', 'attempts': 2}}
        self.assertTrue(allowed(state, 'ask_caller', {'field': 'verification_factor'}))
        self.assertFalse(allowed(state, 'find_appointments', {'patient_id': 'invented'}))
        state['verification']['attempts'] = 3
        self.assertTrue(allowed(state, 'request_handoff', {'reason': 'verification_failed'}))
        self.assertFalse(allowed(state, 'ask_caller', {'field': 'verification_factor'}))
        self.assertFalse(allowed(state, 'get_clinic_info', {'field': 'fees'}))

    def test_already_retrieved_multiple_appointments_require_selection(self):
        state = BASE | {'task': 'cancel', 'stage': 'select_appointment',
                        'verification': {'status': 'verified', 'patient_id': 'person_72'}}
        self.assertTrue(allowed(state, 'find_appointments', {'patient_id': 'person_72'}))
        self.assertFalse(allowed(state, 'find_appointments', {'patient_id': 'another_person'}))
        state['appointments'] = ['visit_3', 'visit_4']
        self.assertTrue(allowed(state, 'ask_caller', {'field': 'appointment'}))
        self.assertFalse(allowed(state, 'find_appointments', {'patient_id': 'person_72'}))

    def test_mutation_requires_confirmed_fields_and_exact_server_identifiers(self):
        state = BASE | {'stage': 'commit', 'subject_patient_id': 'person_72', 'held_slot': 'hold_6',
                        'service': 'dental', 'idempotency_key': 'key_17', 'critical_fields_confirmed': False}
        args = {'patient_id': 'person_72', 'slot': 'hold_6', 'service': 'dental', 'idempotency_key': 'key_17'}
        self.assertFalse(allowed(state, 'book_appointment', args))
        state['critical_fields_confirmed'] = True
        self.assertTrue(allowed(state, 'book_appointment', args))
        self.assertFalse(allowed(state, 'book_appointment', args | {'idempotency_key': 'new_key'}))
        moved = state | {'task': 'reschedule', 'owned_appointment': 'visit_3', 'policy_allowed': True,
                         'verification': {'status': 'verified', 'patient_id': 'person_72'}}
        move = {'appointment_id': 'visit_3', 'new_slot': 'hold_6', 'idempotency_key': 'key_17'}
        self.assertTrue(allowed(moved, 'reschedule_appointment', move))
        self.assertFalse(allowed(moved | {'policy_allowed': False}, 'reschedule_appointment', move))
        self.assertFalse(allowed(moved | {'verification': {'status': 'unverified'}}, 'reschedule_appointment', move))

    def test_known_provider_subject_and_parked_task_updates_do_not_grant_verification(self):
        state = BASE | {'providers': {'Doctor Example': 'provider_97'}, 'family_subjects': {'son': 'person_14'}}
        self.assertTrue(allowed(state, 'update_dialogue_state', {'patch': {'provider_id': 'provider_97'}}))
        self.assertTrue(allowed(state, 'update_dialogue_state', {'patch': {'subject_patient_id': 'person_14'}}))
        self.assertTrue(allowed(state, 'update_dialogue_state', {'patch': {'task': 'cancel', 'park_current': True}}))
        for patch in [{}, {'provider_id': 'unknown'}, {'subject_patient_id': 'unknown'},
                      {'verification': 'verified'}, {'task': 'cancel', 'park_current': False}]:
            self.assertFalse(allowed(state, 'update_dialogue_state', {'patch': patch}))

    def test_search_and_readonly_digressions_remain_available_without_invented_filters(self):
        state = BASE | {'stage': 'search', 'service': 'dental', 'date_from': '2028-02-19', 'date_to': '2028-02-20'}
        args = {k: state[k] for k in ['service', 'date_from', 'date_to']}
        self.assertTrue(allowed(state, 'get_available_slots', args))
        self.assertFalse(allowed(state, 'get_available_slots', args | {'service': 'invented'}))
        self.assertTrue(allowed(state, 'get_clinic_info', {'field': 'fees'}))
        self.assertFalse(allowed(state, 'ask_caller', {'field': 'time'}))

    def test_emergency_and_server_failure_causes_take_priority(self):
        for state, name, args in [
            (BASE | {'emergency_detected': True}, 'speak_template', {'template_id': 'emergency'}),
            (BASE | {'tool_error': 'not_owner'}, 'speak_template', {'template_id': 'not_owner'}),
            (BASE | {'tool_error': 'upstream_timeout', 'retry_count': 1}, 'request_handoff', {'reason': 'upstream_timeout'}),
            (BASE | {'tool_error': 'no_answer'}, 'request_handoff', {'reason': 'no_answer'}),
        ]:
            self.assertTrue(allowed(state, name, args))
            self.assertFalse(allowed(state, 'request_handoff', {'reason': 'caller_requested'}))

    def test_all_original_oracles_remain_eligible_but_never_enter_the_request(self):
        for probe in load_probes('all'):
            if probe['suite'] != 'actions':
                continue
            with self.subTest(id=probe['id']):
                schema = state_action_schema(probe['state'], probe['input'])
                self.assertTrue(Draft202012Validator(schema).is_valid(probe['expected']))
                changed = copy.deepcopy(probe)
                changed['expected'] = {'name': 'DO_NOT_SEND', 'arguments': {}}
                changed['id'] = changed['scenario_id'] = changed['rationale'] = 'DO_NOT_SEND'
                self.assertEqual(build_probe_payload('nvidia/nemotron-3.5-bolt-e4b', probe, action_policy='state_constrained'),
                                 build_probe_payload('nvidia/nemotron-3.5-bolt-e4b', changed, action_policy='state_constrained'))

    def test_oversized_input_and_non_fixture_state_fail_closed(self):
        for state, text in [(BASE, 'word ' * 41), (BASE | {'fixture_only': False}, 'tomorrow')]:
            with self.assertRaises(ValueError):
                state_action_schema(state, text)

    def test_decode_rejects_a_globally_valid_but_ineligible_action_without_repair(self):
        schema = state_action_schema(BASE | {'date': '2028-02-19', 'awaiting': 'time'}, 'Please continue.')
        response = {'content': '{"name":"ask_caller","arguments":{"field":"confirmation"}}',
                    'tools': [], 'reasoning_chars': 0, 'total_ms': 1}
        self.assertEqual(decode_action_response(response)['tools'][0]['name'], 'ask_caller')
        with self.assertRaises(ValueError):
            decode_action_response(response, schema)

    def test_state_is_not_mutated_and_native_protocol_cannot_bypass_eligibility(self):
        probe = load_probes()[0]
        original = copy.deepcopy(probe)
        build_probe_payload('nvidia/nemotron-3.5-bolt-e4b', probe, action_policy='state_constrained')
        self.assertEqual(probe, original)
        with self.assertRaises(ValueError):
            build_probe_payload('nvidia/nemotron-3.5-bolt-e4b', probe, 'native', 'state_constrained')

    def test_mismatched_verified_subject_cannot_lookup_cancel_or_reschedule(self):
        state = BASE | {'task': 'reschedule', 'stage': 'commit', 'subject_patient_id': 'person_other',
                        'verification': {'status': 'verified', 'patient_id': 'person_72'},
                        'critical_fields_confirmed': True, 'policy_allowed': True,
                        'owned_appointment': 'visit_3', 'held_slot': 'hold_6', 'idempotency_key': 'key_17'}
        self.assertFalse(allowed(state, 'reschedule_appointment', {
            'appointment_id': 'visit_3', 'new_slot': 'hold_6', 'idempotency_key': 'key_17'}))
        self.assertFalse(allowed(state | {'task': 'cancel'}, 'cancel_appointment', {
            'appointment_id': 'visit_3', 'idempotency_key': 'key_17'}))
        for patient in ['person_other', 'person_72']:
            self.assertFalse(allowed(state | {'stage': 'select_appointment'}, 'find_appointments', {'patient_id': patient}))

    def test_invalid_late_eligibility_is_rejected_before_credentials_or_inference(self):
        probes = copy.deepcopy(load_probes())
        next(p for p in reversed(probes) if p['suite'] == 'actions')['state']['retry_count'] = -1
        with patch('evaluation.flows.load_probes', return_value=probes), \
             patch('evaluation.flows.load_env', side_effect=AssertionError('No credentials before preflight')), \
             patch('evaluation.flows.request_stream', side_effect=AssertionError('No inference before preflight')), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--action-policy', 'state_constrained']), 2)


if __name__ == '__main__':
    unittest.main()

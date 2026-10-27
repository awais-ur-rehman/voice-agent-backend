"""Evaluation-only eligibility from supplied state, not a production authorization layer.

No fixture expectations are consumed and no state is mutated or tool executed.
The unrestricted thirteen-action diagnostic remains available separately.
"""

import re

from .action_protocol import action_schema


def fixed_arguments(values: dict) -> dict:
    return {'type': 'object', 'properties': {key: {'const': value} for key, value in values.items()},
            'required': list(values), 'additionalProperties': False}


def literal_spans(text: str) -> list[str]:
    """Bounded contiguous word spans retain the caller's actual characters/script."""
    words = list(re.finditer(r"\w+(?:[-'/]\w+)*", text, re.UNICODE))
    if not 1 <= len(words) <= 40:
        raise ValueError('The synthetic eligibility experiment supports one to forty words')
    return list(dict.fromkeys(text[start.start():end.end()]
                             for index, start in enumerate(words) for end in words[index:]))


def state_action_schema(state: dict, text: str) -> dict:
    if state.get('fixture_only') is not True or state.get('state_version') != 1:
        raise ValueError('Eligibility is restricted to version-one synthetic state')
    spans = literal_spans(text)
    registry = {branch['properties']['name']['const']: branch for branch in action_schema()['anyOf']}
    candidates = []

    def allow(name: str, arguments: dict) -> None:
        branch = registry[name]
        # Every name is added at most once, and action_schema returns fresh objects.
        branch['properties']['arguments'] = arguments
        candidates.append(branch)

    def only(name: str, arguments: dict) -> dict:
        allow(name, fixed_arguments(arguments))
        return {'anyOf': candidates}

    verification = state.get('verification', {})
    if not isinstance(verification, dict):
        raise ValueError('Expected structured synthetic verification')
    attempts = verification.get('attempts', 0)
    retry_count = state.get('retry_count', 0)
    if (type(attempts) is not int or attempts < 0
            or type(retry_count) is not int or retry_count < 0):
        raise ValueError('Expected nonnegative integer attempt counters')
    verified = verification.get('status') == 'verified'
    error = state.get('tool_error')
    if state.get('emergency_detected') is True:
        return only('speak_template', {'template_id': 'emergency'})
    if error == 'not_owner':
        return only('speak_template', {'template_id': 'not_owner'})
    if attempts >= 3 and not verified:
        return only('request_handoff', {'reason': 'verification_failed'})
    if error == 'upstream_timeout' and retry_count >= 1:
        return only('request_handoff', {'reason': 'upstream_timeout'})
    if error == 'no_answer':
        return only('request_handoff', {'reason': 'no_answer'})

    allow('get_clinic_info', registry['get_clinic_info']['properties']['arguments'])
    allow('request_handoff', fixed_arguments({'reason': 'caller_requested'}))

    updates = [fixed_arguments({'date_phrase': ''})]
    updates[0]['properties']['date_phrase'] = {'type': 'string', 'enum': spans}
    patches = []
    for field, mapping in [('provider_id', 'providers'), ('subject_patient_id', 'family_subjects')]:
        values = state.get(mapping, {})
        if not isinstance(values, dict) or any(not isinstance(v, str) or not v for v in values.values()):
            raise ValueError('Expected a supplied name-to-ID map')
        if values:
            patch = fixed_arguments({field: ''})
            patch['properties'][field] = {'type': 'string', 'enum': list(dict.fromkeys(values.values()))}
            patches.append(patch)
    task_patch = fixed_arguments({'task': '', 'park_current': True})
    task_patch['properties']['task'] = {'enum': [task for task in ['book', 'cancel', 'reschedule']
                                                if task != state.get('task')]}
    patches.append(task_patch)
    patch_update = fixed_arguments({'patch': {}})
    patch_update['properties']['patch'] = {'oneOf': patches}
    updates.append(patch_update)
    allow('update_dialogue_state', {'oneOf': updates})

    task, stage = state.get('task'), state.get('stage')
    question_fields = []
    if stage == 'collect_need' and state.get('awaiting') == 'time' and state.get('date'):
        question_fields.append('time')
    if stage in {'confirm', 'commit'} and state.get('critical_fields_confirmed') is False:
        question_fields.append('confirmation')
    if task in {'cancel', 'reschedule'} and stage == 'verify' and not verified:
        question_fields.append('verification_factor')
    appointments = state.get('appointments')
    if stage == 'select_appointment' and verified:
        if isinstance(appointments, list) and len(appointments) > 1:
            question_fields.append('appointment')
        elif appointments is None and isinstance(verification.get('patient_id'), str):
            subject = state.get('subject_patient_id', verification['patient_id'])
            if subject == verification['patient_id']:
                allow('find_appointments', fixed_arguments({'patient_id': subject}))
    if question_fields:
        question = fixed_arguments({'field': ''})
        question['properties']['field'] = {'enum': question_fields}
        allow('ask_caller', question)

    if stage == 'identify' and isinstance(state.get('synthetic_phone'), str):
        allow('find_patient', fixed_arguments({'phone': state['synthetic_phone']}))
    if (stage == 'collect_patient' and state.get('lookup') == 'patient_not_found'
            and state.get('confirmed_name') and state.get('confirmed_phone')):
        allow('create_patient', fixed_arguments({'name': state['confirmed_name'], 'phone': state['confirmed_phone']}))
    if stage == 'search' and all(isinstance(state.get(key), str) and state[key]
                                 for key in ['service', 'date_from', 'date_to']):
        allow('get_available_slots', fixed_arguments({key: state[key] for key in ['service', 'date_from', 'date_to']}))
    if stage == 'offer' and isinstance(state.get('offered_slots'), list) and state['offered_slots']:
        slots = state['offered_slots']
        if any(not isinstance(slot, str) or not slot for slot in slots):
            raise ValueError('Expected supplied slot identifiers')
        slot_args = fixed_arguments({'slot_id': ''})
        slot_args['properties']['slot_id'] = {'enum': list(dict.fromkeys(slots))}
        allow('hold_slot', slot_args)
    if stage == 'commit' and state.get('critical_fields_confirmed') is True:
        if task == 'book' and all(isinstance(state.get(key), str) and state[key]
                                  for key in ['subject_patient_id', 'held_slot', 'service', 'idempotency_key']):
            allow('book_appointment', fixed_arguments({'patient_id': state['subject_patient_id'],
                  'slot': state['held_slot'], 'service': state['service'], 'idempotency_key': state['idempotency_key']}))
        if (task in {'cancel', 'reschedule'} and verified and state.get('policy_allowed') is True
                and verification.get('patient_id')
                and state.get('subject_patient_id', verification['patient_id']) == verification['patient_id']
                and all(isinstance(state.get(key), str) and state[key] for key in ['owned_appointment', 'idempotency_key'])):
            arguments = {'appointment_id': state['owned_appointment'], 'idempotency_key': state['idempotency_key']}
            if task == 'cancel':
                allow('cancel_appointment', fixed_arguments(arguments))
            elif isinstance(state.get('held_slot'), str) and state['held_slot']:
                allow('reschedule_appointment', fixed_arguments(arguments | {'new_slot': state['held_slot']}))
    return {'anyOf': candidates}

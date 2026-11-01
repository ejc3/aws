"""Offline actual triage-contract tests: no SDK client, network or subprocess."""
import copy
import json
import socket
import time
import unittest
from unittest.mock import patch

import triage

CVE = 'CVE-2026-12345'
PURL = 'pkg:deb/ubuntu/example@1.2-0ubuntu1?arch=arm64&distro=noble'
SECRET = 'sk-ant-secret-must-not-leak'


def case():
    return {'cve': CVE, 'known_exploit': 'NO', 'fix_available': 'NO', 'severity': 'CRITICAL',
        'targets': [{'instance_id': 'i-0123456789abcdef0'}],
        'packages': [{'instance_id': 'i-0123456789abcdef0', 'origin': 'ubuntu_archive', 'purl': PURL}],
        'description': SECRET, 'url': 'http://169.254.169.254/latest/meta-data/', 'token': SECRET}


def evidence(status='not_affected', justification='vulnerable_code_not_present', notes='', age=0):
    document = {'author': 'Canonical Ltd.', '@id': 'https://github.com/canonical/ubuntu-security-notices/blob/main/vex/cve/2026/' + CVE,
        'statements': [{'vulnerability': {'name': CVE, 'description': SECRET},
            'products': [{'@id': PURL}], 'status': status, 'justification': justification, 'status_notes': notes}]}
    return triage.VendorEvidence(CVE, time.time() - age, json.dumps(document).encode())


class FakeClient:
    def __init__(self, **change):
        self.calls = []
        self.value = {'decision': 'defer', 'category': 'not_applicable', 'confidence': 0.99, 'reason': SECRET}
        self.value.update(change)
        self.stop_reason = 'end_turn'

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {'output': {'message': {'role': 'assistant', 'content': [{'text': json.dumps(self.value)}]}},
            'stopReason': self.stop_reason, 'usage': {'inputTokens': 123, 'outputTokens': 30}}


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.sdk = patch.object(triage, '_client', side_effect=AssertionError('real SDK forbidden'))
        self.sdk.start()
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.fetch = patch.object(triage, 'fetch_vendor', side_effect=AssertionError('vendor network forbidden'))
        self.fetch.start()

    def tearDown(self):
        self.fetch.stop()
        self.network.stop()
        self.sdk.stop()

    def test_actual_verified_not_applicable_defers_without_resolving(self):
        client = FakeClient()
        result = triage.classify(case(), evidence(), client)
        self.assertEqual(result['decision'], 'defer')
        self.assertEqual(result['recheck_after_seconds'], 3600)
        self.assertTrue(result['notify'])
        self.assertFalse(result['resolved'])
        self.assertFalse(result['suppressed'])
        self.assertEqual(result['cost']['output_tokens'], 30)
        self.assertIsNone(result['cost']['usd'])
        self.assertEqual(client.calls[0]['modelId'], triage.MODEL)
        self.assertEqual(client.calls[0]['inferenceConfig']['maxTokens'], 800)
        self.assertEqual(set(client.calls[0]['inferenceConfig']), {'maxTokens'})
        self.assertNotIn('toolConfig', client.calls[0])

    def test_waiting_vendor_requires_exact_explicit_deferred_statement(self):
        for notes, decision in [('The vulnerability is known but remediation is deferred.', 'defer'),
                                ('Vulnerability assessment is pending.', 'run_doctor'), ('', 'run_doctor')]:
            with self.subTest(notes=notes):
                client = FakeClient(category='waiting_for_vendor')
                self.assertEqual(triage.classify(case(), evidence('under_investigation', notes=notes), client)['decision'], decision)

    def test_mitigation_requires_primary_inline_mitigation_not_model_claim(self):
        self.assertEqual(triage.classify(case(), evidence(justification='inline_mitigations_already_exist'),
            FakeClient(category='already_mitigated'))['decision'], 'defer')
        self.assertEqual(triage.classify(case(), evidence('affected'),
            FakeClient(category='already_mitigated'))['decision'], 'run_doctor')

    def test_exploit_or_fix_yes_overrides_model_and_skips_call(self):
        for key in ['known_exploit', 'fix_available']:
            for value in [True, 'YES']:
                with self.subTest(key=key, value=value):
                    facts = case(); facts[key] = value
                    client = FakeClient()
                    self.assertEqual(triage.classify(facts, evidence(), client)['decision'], 'run_doctor')
                    self.assertEqual(client.calls, [])

    def test_unknown_flags_missing_vendor_plain_dict_stale_or_future_never_defer(self):
        facts = case(); facts.pop('known_exploit')
        for data, vendor in [(facts, evidence()), (case(), None), (case(), {'verified': True}),
                             (case(), evidence(age=3601)), (case(), evidence(age=-60))]:
            with self.subTest(vendor=type(vendor).__name__):
                client = FakeClient()
                self.assertEqual(triage.classify(data, vendor, client)['decision'], 'run_doctor')
                self.assertEqual(client.calls, [])

    def test_target_version_origin_or_coverage_mismatch_needs_doctor(self):
        for mutate in [lambda x:x['packages'][0].update(purl=PURL.replace('1.2-', '1.3-')),
                       lambda x:x['packages'][0].update(origin='custom_build'),
                       lambda x:x['targets'].append({'instance_id': 'i-11111111111111111'}),
                       lambda x:x['targets'][0].update(instance_id='i-0123456789'),
                       lambda x:x['packages'][0].update(purl=PURL.replace('noble', 'jammy'))]:
            facts = case(); mutate(facts)
            client = FakeClient()
            self.assertEqual(triage.classify(facts, evidence(), client)['decision'], 'run_doctor')
            self.assertEqual(client.calls, [])

    def test_released_unknown_or_conflicting_vendor_state_needs_doctor(self):
        for status in ['fixed', 'affected', 'unknown', 'under_investigation']:
            self.assertEqual(triage.classify(case(), evidence(status), FakeClient())['decision'], 'run_doctor')
        vendor = evidence()
        document = json.loads(vendor.body)
        other = copy.deepcopy(document['statements'][0]); other['status'] = 'fixed'
        document['statements'].append(other)
        vendor = triage.VendorEvidence(CVE, time.time(), json.dumps(document).encode())
        self.assertEqual(triage.classify(case(), vendor, FakeClient())['decision'], 'run_doctor')

    def test_model_errors_timeouts_and_malformed_results_default_doctor(self):
        for change in [{'confidence': 0.94}, {'confidence': True}, {'confidence': float('nan')},
                       {'category': 'already_mitigated'}, {'decision': 'suppress'}, {'script': 'rm -rf /'}]:
            with self.subTest(change=change):
                result = triage.classify(case(), evidence(), FakeClient(**change))
                self.assertEqual(result['decision'], 'run_doctor')
        class Broken:
            def converse(self, **kwargs): raise TimeoutError(SECRET)
        result = triage.classify(case(), evidence(), Broken())
        self.assertEqual(result['decision'], 'run_doctor')
        self.assertTrue(result['cost']['request_attempted'])
        self.assertIsNone(result['cost']['input_tokens'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_token_limit_tool_response_and_duplicate_json_are_refused(self):
        for variant in ['truncated', 'tools', 'duplicate', 'tokens']:
            client = FakeClient()
            original = client.converse
            def response(**kwargs):
                data = original(**kwargs)
                if variant == 'truncated': data['stopReason'] = 'max_tokens'
                if variant == 'tools': data['output']['message']['content'] = [{'toolUse': {'name': 'script'}}]
                if variant == 'duplicate': data['output']['message']['content'][0]['text'] = '{"decision":"defer","decision":"run_doctor"}'
                if variant == 'tokens': data['usage']['outputTokens'] = 801
                return data
            client.converse = response
            self.assertEqual(triage.classify(case(), evidence(), client)['decision'], 'run_doctor')

    def test_redaction_and_input_bound_strip_case_secrets_vendor_prose_and_urls(self):
        client = FakeClient()
        result = triage.classify(case(), evidence(), client)
        self.assertNotIn(SECRET, json.dumps(client.calls))
        self.assertNotIn('169.254', json.dumps(client.calls))
        self.assertNotIn(SECRET, json.dumps(result))
        with patch.object(triage, 'MAX_INPUT_BYTES', 10):
            self.assertEqual(triage.classify(case(), evidence(), client)['decision'], 'run_doctor')
        self.assertEqual(len(client.calls), 1)

    def test_stateless_duplicate_verdicts_have_same_upstream_cache_binding(self):
        vendor, client = evidence(), FakeClient()
        first = triage.classify(case(), vendor, client)
        second = triage.classify(case(), vendor, client)
        self.assertEqual(first['input_sha256'], second['input_sha256'])
        self.assertEqual(len(client.calls), 2)  # Controller, not this function, owns durable caching.

    def test_self_test_is_advisory_and_only_explicit_call_invokes_fake_model(self):
        client = FakeClient(decision='run_doctor', category='needs_investigation')
        result = triage.self_test(client)
        self.assertTrue(result['ok'])
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn('decision', result)

    def test_fixed_vendor_url_rejects_event_ssrf_and_private_dns(self):
        for value in ['https://169.254.169.254/', '../CVE-2026-12345', CVE + '?url=x', None]:
            with self.subTest(value=value), self.assertRaises(ValueError): triage.vendor_url(value)
        self.assertEqual(triage.vendor_url(CVE), triage.ROOT + '2026/' + CVE + '.json')
        connection = triage._PublicHTTPS(triage.HOST, timeout=5)
        with patch.object(socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), self.assertRaises(ValueError):
            connection.connect()

    def test_vendor_fetch_rejects_redirect_oversize_encoding_and_restores_no_proxy(self):
        self.fetch.stop()
        class Response:
            status = 200
            encoding = 'identity'
            raw = evidence().body
            def getheader(self, key, default): return self.encoding
            def read(self, limit): self.limit = limit; return self.raw[:limit]
        class Connection:
            sock = None
            def __init__(self, *args, **kwargs): self.response = response
            def request(self, *args, **kwargs): requests.append((args, kwargs))
            def getresponse(self): return self.response
            def close(self): closed.append(True)
        for status, encoding, raw, ok in [(200, 'identity', evidence().body, True),
                (302, 'identity', b'{}', False), (200, 'gzip', b'{}', False),
                (200, 'identity', b'x' * 65537, False)]:
            response = Response(); response.status, response.encoding, response.raw = status, encoding, raw
            requests, closed = [], []
            with patch.object(triage, '_PublicHTTPS', Connection):
                if ok: self.assertIsInstance(triage.fetch_vendor(CVE), triage.VendorEvidence)
                else:
                    with self.assertRaises(ValueError): triage.fetch_vendor(CVE)
            self.assertEqual(closed, [True])
            self.assertEqual(requests[0][0][1], '/canonical/ubuntu-security-notices/main/vex/cve/2026/' + CVE + '.json')
        self.fetch.start()


if __name__ == '__main__':
    unittest.main(buffer=True)

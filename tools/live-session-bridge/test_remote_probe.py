"""Regression coverage for meaningful replay comparisons and input guards."""
from __future__ import annotations
import copy
import unittest
from remote_probe import WebSocket,ProbeFailure,conversation_fingerprint,notification_summary,event_ids

F={'sessionId':'fixture'}
def event(text='test',client='a',kind='user_message_chunk',event_id='1'):
    return {'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fixture','_meta':{'eventId':event_id,'x.ai/leaderClientId':client},'update':{'sessionUpdate':kind,'content':{'type':'text','text':text}}}}

class ProbeTests(unittest.TestCase):
    def test_per_connection_client_identity_is_not_conversation_content(self):
        self.assertEqual(conversation_fingerprint([event(client='a')],F),conversation_fingerprint([event(client='b')],F))
    def test_actual_message_change_is_detected(self):
        self.assertNotEqual(conversation_fingerprint([event(text='before')],F),conversation_fingerprint([event(text='after')],F))
    def test_event_identity_change_is_detected(self):
        self.assertNotEqual(conversation_fingerprint([event(event_id='1')],F),conversation_fingerprint([event(event_id='2')],F))
    def test_background_snapshot_does_not_change_conversation(self):
        a=[event()];b=a+[event(kind='background_tasks',event_id='2')]
        self.assertEqual(conversation_fingerprint(a,F),conversation_fingerprint(b,F))
        self.assertTrue(event_ids(a,F)<=event_ids(b,F))
    def test_duplicate_replay_event_ids_are_detected(self):
        self.assertEqual(notification_summary([event(),event()],F)['duplicate_event_ids'],1)
    def test_different_session_is_excluded(self):
        another=event();another['params']['sessionId']='other'
        self.assertEqual(conversation_fingerprint([event()],F),conversation_fingerprint([event(),another],F))
    def test_insecure_endpoint_is_rejected_before_network(self):
        with self.assertRaisesRegex(ProbeFailure,'invalid_secure_endpoint'):WebSocket('ws://public.invalid/ws','test')
    def test_header_injection_is_rejected_before_network(self):
        with self.assertRaisesRegex(ProbeFailure,'invalid_credential'):WebSocket('wss://public.invalid/ws','test\r\nInjected: true')

if __name__=='__main__':unittest.main()

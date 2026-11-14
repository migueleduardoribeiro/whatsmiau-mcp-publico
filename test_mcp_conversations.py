import unittest
from pathlib import Path
from mcp_conversation_query import ConversationQuery
from mcp_conversation_store import ConversationStore
from mcp_read_coordinator import ReadCoordinator
from mcp_tools import Tools
from mcp_oauth import Principal
from test_mcp_conversation_catalog import CatalogTests


class ConversationToolTests(CatalogTests):
    def setUp(self):
        super().setUp()
        self.state = ConversationStore(Path(self.tmp.name)/'effects.db'); self.addCleanup(self.state.close)
        self.services=dict(catalog=self.catalog, query=ConversationQuery(self.archive,self.catalog),
            reader=ReadCoordinator(self.archive,self.source,self.state))
        self.tools=Tools(self.bridge,self.store,{'instance_id':'personal'},archive=self.archive,
                         conversations=self.services)
        self.principal=Principal('owner','personal',frozenset({'whatsapp.read'}))
        self.chat='5511999999999@s.whatsapp.net'

    def call(self,name,args,principal=None):
        return self.tools.call(name,args,principal or self.principal)

    def test_feature_disabled_keeps_legacy_schema_and_annotations(self):
        tools=Tools(self.bridge,self.store,{'instance_id':'personal'},archive=self.archive)
        definitions={tool['name']:tool for tool in tools.list_tools()}
        self.assertNotIn('consultar_recentes',definitions)
        self.assertTrue(definitions['consultar_mensagens']['annotations']['readOnlyHint'])

    def test_enabled_vision_does_not_enable_conversations(self):
        from unittest.mock import Mock, patch
        from server import configure_mcp
        config=dict(issuer='https://fixture.example',instance_id='personal',allowed_user='owner',clients={},
            state_path=str(Path(self.tmp.name)/'runtime.db'),enable_media=True,media_fetch_token='fake',archive={},
            vision=dict(enabled=True,authorized_at='test',free_plan_verified_at='test'),
            transcription=dict(enabled=True,key_path='unused',proxy_token='fake'))
        runtime=Mock(query=self.archive)
        with patch('mcp_archive_runtime.ArchiveRuntime',return_value=runtime), \
             patch('mcp_media.MediaProvider'),patch('mcp_files.FileGateway'), \
             patch('mcp_vision.GroqVisionClient'),patch('mcp_vision.ImageAnalyzer'):
            app=configure_mcp(config,self.bridge)
        self.addCleanup(app.oauth.store.close)
        if app.conversation_store is not None: self.addCleanup(app.conversation_store.close)
        self.assertIsNone(app.tools.conversations)

    def test_listing_returns_metadata_and_zero_receipts(self):
        self.add(self.chat)
        result=self.call('listar_conversas',{})
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['conversations'][0]['chat'],self.chat)
        self.assertNotIn('text',str(result['structuredContent']))
        self.assertFalse(any(call[1] != 'GET' for call in self.bridge.calls))
        spec=next(d for d in self.tools.list_tools() if d['name']=='listar_conversas')
        self.assertTrue(spec['annotations']['readOnlyHint'])

    def test_recent_summary_marks_only_returned_content_and_replays(self):
        self.add(self.chat,'one'); self.add(self.chat,'two')
        args=dict(conversas=[self.chat],inicio='1970-01-01T00:00:01Z',fim='2099-01-01T00:00:00Z',
                  limite=1,operacao_id='recent-1')
        first=self.call('consultar_recentes',args)
        self.assertFalse(first['isError'])
        self.assertEqual([m['id'] for m in first['structuredContent']['mensagens']],['two'])
        self.assertEqual(len([call for call in self.bridge.calls if call[0].endswith('/read-messages')]),1)
        second=self.call('consultar_recentes',args)
        self.assertEqual(first['structuredContent'],second['structuredContent'])
        self.assertEqual(len([call for call in self.bridge.calls if call[0].endswith('/read-messages')]),1)
        spec=next(d for d in self.tools.list_tools() if d['name']=='consultar_recentes')
        self.assertFalse(spec['annotations']['readOnlyHint'])
        self.assertEqual(spec['securitySchemes'][0]['scopes'],['whatsapp.read'])

    def test_unavailable_source_does_not_claim_empty_unread(self):
        self.add(self.chat)
        result=self.call('consultar_nao_lidas',dict(conversas=[self.chat],operacao_id='unread-1'))
        self.assertFalse(result['isError'])
        value=result['structuredContent']
        self.assertEqual(value['read_state']['value'],'unknown')
        self.assertEqual(value['mensagens'],[])
        self.assertEqual(value['reason'],'whatsapp_read_state_unverified')
        self.assertFalse(any(call[1] != 'GET' for call in self.bridge.calls))

    def test_explicit_degraded_mode_never_calls_available_content_unread(self):
        self.add(self.chat,'available')
        self.services['query'].unread_fallback=True
        args=dict(conversas=[self.chat],operacao_id='degraded-1')
        value=self.call('consultar_nao_lidas',args)['structuredContent']
        self.assertEqual([message['id'] for message in value['mensagens']],['available'])
        self.assertEqual(value['read_state']['value'],'unknown')
        self.assertEqual(value['fallback_mode'],'available_messages')
        self.assertEqual(value['warning'],'Não foi possível confirmar quais mensagens estão não lidas no WhatsApp. Abaixo estão apenas as mensagens disponíveis; elas podem já ter sido lidas.')
        self.assertEqual(self.call('consultar_nao_lidas',args)['structuredContent'],value)

    def test_degraded_mode_configuration_is_explicit_and_validated(self):
        from unittest.mock import Mock,patch
        from server import configure_mcp
        config=dict(issuer='https://fixture.example',instance_id='personal',allowed_user='owner',clients={},
            state_path=str(Path(self.tmp.name)/'runtime.db'),enable_media=True,media_fetch_token='fake',archive={},
            conversations=dict(enabled=True,unread_mode='available_with_warning'))
        with patch('mcp_archive_runtime.ArchiveRuntime',return_value=Mock(query=self.archive)), \
             patch('mcp_media.MediaProvider'),patch('mcp_files.FileGateway'):
            app=configure_mcp(config,self.bridge)
        self.addCleanup(app.oauth.store.close); self.addCleanup(app.conversation_store.close)
        self.assertTrue(app.tools.conversations['query'].unread_fallback)
        self.assertFalse(app.tools.conversations['reader'].source.receipts_enabled)
        definitions={tool['name']:tool for tool in app.tools.list_tools()}
        self.assertTrue(definitions['consultar_recentes']['annotations']['readOnlyHint'])
        config['conversations']['unread_mode']='invent_unread'
        with self.assertRaisesRegex(ValueError,'Invalid conversations setting'):
            configure_mcp(config,self.bridge)

    def test_invalid_chat_or_instance_never_has_effects(self):
        for args in [dict(conversas=[self.chat,'not-a-jid'],operacao_id='bad-1'),
                     dict(conversas=[self.chat],limite=True,operacao_id='bad-1')]:
            self.assertTrue(self.call('consultar_recentes',args)['isError'])
        self.assertTrue(self.call('listar_conversas',{},Principal('owner','other',frozenset({'whatsapp.read'})))['isError'])
        self.assertFalse(any(call[1] != 'GET' for call in self.bridge.calls))

    def test_legacy_history_marks_only_returned_page(self):
        self.add(self.chat)
        value=self.call('consultar_mensagens',dict(destinatario=self.chat,inicio='1970-01-01T00:00:01Z',
            fim='2099-01-01T00:00:00Z',operacao_id='legacy-1'))
        self.assertFalse(value['isError'])
        self.assertEqual(value['structuredContent']['read_result']['status'],'uncertain')
        self.assertEqual(len([call for call in self.bridge.calls if call[0].endswith('/read-messages')]),1)

    def test_pending_continuation_response_loss_replays_same_operation(self):
        from unittest.mock import patch
        from mcp_archive_query import BudgetExceeded
        self.add(self.chat)
        args=dict(conversas=[self.chat],inicio='1970-01-01T00:00:01Z',fim='2099-01-01T00:00:00Z',
                  operacao_id='scan-0')
        with patch.object(self.archive,'_raw',side_effect=BudgetExceeded):
            first=self.call('consultar_recentes',args)['structuredContent']
            continuation=dict(args,cursor=first['cursor'],operacao_id='scan-1')
            lost=self.call('consultar_recentes',continuation)
            repeated=self.call('consultar_recentes',continuation)
        self.assertFalse(repeated['isError'])
        self.assertEqual(repeated['structuredContent'],lost['structuredContent'])
        self.assertEqual(len([c for c in self.bridge.calls if c[0].endswith('/read-messages')]),0)

    def test_batch_cursor_response_loss_replays_before_query(self):
        for identifier in ('a','b','c'): self.add(self.chat,identifier)
        base=dict(conversas=[{'destinatario':self.chat}],inicio='1970-01-01T00:00:01Z',
                  fim='2099-01-01T00:00:00Z',limite_por_conversa=1,operacao_id='batch-0')
        first=self.call('consultar_mensagens_lote',base)['structuredContent']['resultados'][0]['resultado']
        self.assertIsNotNone(first['cursor'])
        args=dict(base,conversas=[dict(destinatario=self.chat,cursor=first['cursor'])],operacao_id='batch-1')
        lost=self.call('consultar_mensagens_lote',args)
        count=len([c for c in self.bridge.calls if c[0].endswith('/read-messages')])
        repeated=self.call('consultar_mensagens_lote',args)
        self.assertNotIn('erro',repeated['structuredContent']['resultados'][0])
        self.assertEqual(lost['structuredContent'],repeated['structuredContent'])
        self.assertEqual(len([c for c in self.bridge.calls if c[0].endswith('/read-messages')]),count)

    def test_recent_explicit_phone_includes_verified_lid_alias(self):
        from unittest.mock import patch
        lid='992222222222222@lid'
        self.add(self.chat,'pn'); self.add(lid,'lid-only')
        original=self.bridge.call
        def mapping(path,method='GET',body=None,timeout=None):
            if '/whatsappNumbers/' in path:
                return [dict(exists=True,jid=self.chat,number=self.chat.split('@')[0],lid=lid)]
            if 'source=identity' in path:
                return dict(requested_chat=self.chat,aliases=[self.chat,lid],canonical_chat=self.chat,resolution_complete=True)
            return original(path,method,body,timeout)
        with patch.object(self.bridge,'call',side_effect=mapping):
            value=self.call('consultar_recentes',dict(conversas=[self.chat],inicio='1970-01-01T00:00:01Z',
                fim='2099-01-01T00:00:00Z',operacao_id='alias-1'))
        self.assertFalse(value['isError'])
        self.assertEqual({m['id'] for m in value['structuredContent']['mensagens']},{'pn','lid-only'})


if __name__=='__main__': unittest.main()

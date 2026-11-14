"""Serialize the final presentation before attempting any WhatsApp read effect."""
from mcp_archive_models import canonical
from mcp_conversation_models import operation, valid_ref


class ReadCoordinator:
    def __init__(self, archive, source, store):
        self.archive, self.source, self.store = archive, source, store

    def _prepare(self, profile, instance, selection, operation_id, read_result):
        refs = selection['refs']
        if not isinstance(refs, list) or len(refs) > 100: raise ValueError('invalid_arguments')
        for ref in refs: valid_ref(ref, instance)
        if refs and selection.get('selection_complete') is not True: raise ValueError('invalid_arguments')
        messages = self.archive.resolve_refs(profile, instance, refs)
        output = {key: value for key, value in selection.items() if key not in ('refs','content_opened')}
        output.update(mensagens=messages, read_result=read_result)
        # Reserve bounded overhead for one outcome per message, before receipts.
        if 3 * len(canonical(output)) + len(refs) * 1024 > 2097152:
            raise ValueError('archive_query_too_broad')
        return output

    def replay(self, profile, instance, operation_id, request):
        self.source.authorize(profile, instance); operation(operation_id)
        saved = self.store.load(profile, instance, operation_id)
        if saved is None: return None
        if saved['intent']['request'] != request: raise ValueError('operation_conflict')
        return self._prepare(profile, instance, saved['intent']['selection'], operation_id, saved['result'])

    def present(self, profile: str, instance: str, selection: dict, operation_id: str, *, request=None):
        self.source.authorize(profile, instance); operation(operation_id)
        initial = dict(status='not_applicable', items=[], operation_id=operation_id)
        output = self._prepare(profile, instance, selection, operation_id, initial)
        intent = dict(selection=selection, request=request)
        previous = self.store.claim(profile, instance, operation_id, intent)
        if previous is not None:
            output['read_result'] = previous
            return output
        visible=[ref for ref,message in zip(selection['refs'],output['mensagens'])
            if selection.get('content_opened') is True or message.get('text') or message.get('location')]
        try:
            outcome = self.source.mark_read(profile, instance, visible, operation_id) if visible else initial
        except Exception:
            outcome = dict(status='uncertain', items=[], operation_id=operation_id, reason='receipt_response_unavailable')
        visible_keys={canonical(ref) for ref in visible}
        outcome['items']+= [dict(ref=ref,status='not_applicable',reason='metadata_only')
            for ref in selection['refs'] if canonical(ref) not in visible_keys]
        try:
            self.store.finish(profile, instance, operation_id, outcome)
        except Exception:
            # The immutable claim remains pending and prevents a second effect.
            # A receipt may already have left; preserve the requested content.
            outcome=dict(status='uncertain',items=[],operation_id=operation_id,reason='read_result_not_persisted')
        output['read_result'] = outcome
        return output

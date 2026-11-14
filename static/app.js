'use strict';
const $ = (id) => document.getElementById(id);
let instances = [], loading = false, lastUpdate = null, qrInstance = null, qrTimer = null, qrBusy = false;
let toastTimer;
let createGeneration = 0, createBusy = false, qrGeneration = 0;

async function api(path, method = 'GET', body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 35000);
  try {
    const response = await fetch('/painel/api' + path, {
      method, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      headers: {'X-LylaHub-Panel': '1', 'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    if (response.status === 401) throw new Error('Sua autenticação expirou. Recarregue a página para entrar novamente.');
    let data;
    try { data = await response.json(); } catch { throw new Error('O serviço está indisponível. Tente novamente em alguns segundos.'); }
    if (!response.ok) throw new Error(data.error || 'Não foi possível concluir a operação.');
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('O serviço demorou a responder. Tente novamente.');
    if (error instanceof TypeError) throw new Error('Não foi possível acessar o serviço. Confira sua conexão.');
    throw error;
  } finally { clearTimeout(timeout); }
}

function showError(id, message) { $(id).textContent = message || ''; $(id).hidden = !message; }
function toast(message) { clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false; toastTimer = setTimeout(() => $('toast').hidden = true, 5000); }
function connected(instance) { return ['open', 'connected'].includes(instance.state); }
function openCreate() {
  createGeneration++; $('create-form').reset(); showError('create-error', '');
  $('create-submit').disabled = createBusy; $('create-submit').textContent = createBusy ? 'Criando…' : 'Criar instância';
  $('create-dialog').showModal(); $('instance-name').focus();
}

function render() {
  const count = instances.length, ready = instances.filter(connected).length;
  $('stat-total').textContent = count; $('stat-connected').textContent = ready; $('stat-pending').textContent = count - ready;
  $('nav-count').textContent = count; $('list-count').textContent = count;
  const query = $('search').value.toLocaleLowerCase('pt-BR').trim();
  const filtered = instances.filter((i) => [i.name, i.number, i.id].some((value) => value.toLocaleLowerCase('pt-BR').includes(query)));
  const list = $('instances'); list.replaceChildren(); list.className = '';
  if (!filtered.length) {
    const empty = document.createElement('div'); empty.className = 'empty';
    const icon = document.createElement('span'); icon.className = 'empty-icon'; icon.textContent = query ? '⌕' : '▦'; icon.setAttribute('aria-hidden', 'true');
    const title = document.createElement('h3'); title.textContent = query ? 'Nenhuma instância encontrada' : 'Tudo pronto para sua primeira conexão';
    const description = document.createElement('p'); description.textContent = query ? 'Tente outro nome, número ou identificador.' : 'Crie sua primeira instância e conecte um número de WhatsApp para começar.';
    empty.append(icon, title, description);
    if (!query) { const button = document.createElement('button'); button.className = 'button primary'; button.textContent = '＋ Criar primeira instância'; button.addEventListener('click', openCreate); empty.append(button); }
    list.append(empty); return;
  }
  list.className = 'cards';
  for (const instance of filtered) {
    const card = $('instance-card').content.cloneNode(true);
    card.querySelector('h3').textContent = instance.name;
    card.querySelector('.instance-number').textContent = instance.number ? '+' + instance.number : 'Nenhum número conectado';
    card.querySelector('.instance-id').textContent = 'ID: ' + instance.id;
    card.querySelector('.instance-id').title = instance.id;
    const badge = card.querySelector('.badge'), button = card.querySelector('.card-connect');
    if (connected(instance)) {
      badge.textContent = 'Conectada'; badge.classList.add('connected'); button.textContent = '✓ WhatsApp conectado'; button.classList.add('connected'); button.disabled = true;
    } else {
      badge.textContent = instance.state === 'connecting' ? 'Conectando' : instance.state === 'unknown' ? 'Status indisponível' : 'Aguardando conexão';
      if (instance.state === 'unknown') badge.classList.add('unknown');
      button.addEventListener('click', () => openQR(instance));
    }
    card.querySelector('.card-webhook').addEventListener('click', () => openWebhook(instance));
    card.querySelector('.card-info').addEventListener('click', () => openInfo(instance));
    card.querySelector('.card-logout').addEventListener('click', () => openAction(instance, 'logout'));
    card.querySelector('.card-delete').addEventListener('click', () => openAction(instance, 'delete'));
    list.append(card);
  }
}

async function loadInstances() {
  if (loading) return;
  loading = true; document.body.classList.add('busy'); $('refresh').disabled = true;
  try {
    const data = await api('/instances');
    instances = data.instances; lastUpdate = new Date(); render(); showError('list-error', '');
    $('sync-dot').className = 'dot green';
    $('sync-label').textContent = 'Atualizado às ' + lastUpdate.toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  } catch (error) {
    showError('list-error', error.message); $('sync-dot').className = 'dot amber'; $('sync-label').textContent = 'Não foi possível atualizar';
    if (!lastUpdate) {
      const empty = document.createElement('div'); empty.className = 'empty';
      const title = document.createElement('h3'); title.textContent = 'Não foi possível carregar';
      const text = document.createElement('p'); text.textContent = 'Use Atualizar para tentar novamente.'; empty.append(title, text); $('instances').replaceChildren(empty);
    }
  } finally { loading = false; document.body.classList.remove('busy'); $('refresh').disabled = false; }
}

$('create-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = $('create-submit'); if (createBusy) return;
  const generation = createGeneration; createBusy = true;
  button.disabled = true; button.textContent = 'Criando…'; showError('create-error', '');
  try {
    const instance = await api('/instances', 'POST', {name: $('instance-name').value.trim()});
    toast('Instância criada. Agora conecte seu WhatsApp.');
    // Creation and pairing are separate: opening the dialog follows this explicit user action.
    if (generation === createGeneration && $('create-dialog').open) { $('create-dialog').close(); openQR(instance); }
    loadInstances();
  } catch (error) {
    if (generation === createGeneration && $('create-dialog').open) showError('create-error', error.message);
    else toast(error.message);
  }
  finally { createBusy = false; button.disabled = false; button.textContent = 'Criar instância'; }
});
$('create-dialog').addEventListener('close', () => { createGeneration++; });

function openQR(instance) {
  qrGeneration++;
  clearTimeout(qrTimer); qrInstance = instance; $('qr-image').hidden = true; $('qr-image').removeAttribute('src');
  $('qr-placeholder').hidden = false; $('qr-placeholder').replaceChildren();
  const loader = document.createElement('span'); loader.className = 'loader';
  const text = document.createElement('p'); text.textContent = 'Preparando o QR code…'; $('qr-placeholder').append(loader, text);
  $('qr-instance-name').textContent = instance.name; $('qr-status').textContent = 'Aguarde alguns segundos.';
  showError('qr-error', ''); $('qr-refresh').hidden = false; $('qr-dialog').showModal(); refreshQR();
}

async function refreshQR() {
  if (!qrInstance || !$('qr-dialog').open || document.hidden) return;
  if (qrBusy) { clearTimeout(qrTimer); qrTimer = setTimeout(refreshQR, 500); return; }
  clearTimeout(qrTimer); const identifier = qrInstance.id, generation = qrGeneration; qrBusy = true; $('qr-refresh').disabled = true;
  try {
    const data = await api('/instances/' + encodeURIComponent(identifier) + '/connect', 'POST', {});
    if (!qrInstance || generation !== qrGeneration || identifier !== qrInstance.id || !$('qr-dialog').open) return;
    showError('qr-error', '');
    if (data.connected) {
      $('qr-image').hidden = true; $('qr-image').removeAttribute('src'); $('qr-placeholder').hidden = false;
      const icon = document.createElement('span'); icon.className = 'success-icon'; icon.textContent = '✓';
      $('qr-placeholder').replaceChildren(icon); $('qr-status').textContent = 'WhatsApp conectado com sucesso!'; $('qr-refresh').hidden = true;
      toast('WhatsApp conectado. Sua instância está pronta.'); loadInstances(); return;
    }
    if (data.base64) {
      $('qr-image').src = 'data:image/png;base64,' + data.base64; $('qr-image').hidden = false; $('qr-placeholder').hidden = true;
      $('qr-status').textContent = 'Escaneie o QR code. Ele será atualizado automaticamente.';
    } else {
      $('qr-image').hidden = true; $('qr-placeholder').hidden = false; $('qr-status').textContent = 'Gerando um novo QR code. Aguarde…';
    }
    qrTimer = setTimeout(refreshQR, 5000);
  } catch (error) {
    if (generation === qrGeneration && qrInstance?.id === identifier && $('qr-dialog').open) {
      showError('qr-error', error.message); $('qr-image').hidden = true; $('qr-status').textContent = 'Tente atualizar o QR code.';
      qrTimer = setTimeout(refreshQR, 10000);
    }
  } finally {
    qrBusy = false; $('qr-refresh').disabled = false;
    if (generation !== qrGeneration && $('qr-dialog').open) { clearTimeout(qrTimer); qrTimer = setTimeout(refreshQR, 0); }
  }
}

$('qr-dialog').addEventListener('close', () => { qrGeneration++; clearTimeout(qrTimer); qrInstance = null; $('qr-image').removeAttribute('src'); loadInstances(); });
$('create-open').addEventListener('click', openCreate);
$('refresh').addEventListener('click', loadInstances);
$('search').addEventListener('input', () => { if (lastUpdate) render(); });
$('qr-refresh').addEventListener('click', refreshQR);
document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => $(button.dataset.close).close()));
document.querySelectorAll('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => {
  const bounds = dialog.getBoundingClientRect();
  if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
}));
document.addEventListener('visibilitychange', () => { if (!document.hidden) { loadInstances(); if ($('qr-dialog').open) refreshQR(); } });
setInterval(() => { if (!document.hidden && !document.querySelector('dialog[open]')) loadInstances(); }, 15000);

let webhookInstance = null, webhookRevision = null, webhookGeneration = 0, webhookBusy = false;
async function openWebhook(instance) {
  const generation = ++webhookGeneration;
  webhookInstance = instance; webhookRevision = null;
  $('webhook-form').reset(); $('webhook-events').replaceChildren();
  $('webhook-instance').textContent = instance.name; $('webhook-note').textContent = 'Consultando configuração atual…';
  $('webhook-submit').disabled = true; $('webhook-fields').disabled = true;
  showError('webhook-error', ''); $('webhook-dialog').showModal();
  try {
    const data = await api('/instances/' + encodeURIComponent(instance.id) + '/webhook');
    if (generation !== webhookGeneration || !$('webhook-dialog').open) return;
    webhookRevision = data.revision;
    $('webhook-url').value = data.webhook.url;
    $('webhook-enabled').checked = data.webhook.enabled;
    $('webhook-base64').checked = data.webhook.base64;
    $('webhook-fields').disabled = false;
    $('webhook-url').readOnly = data.managed;
    $('webhook-enabled').disabled = data.managed;
    $('webhook-base64').disabled = data.managed;
    for (const name of data.supported_events) {
      const label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
      input.type = 'checkbox'; input.value = name; input.checked = data.webhook.events.includes(name);
      input.disabled = data.managed && name === 'MESSAGES_UPSERT';
      text.textContent = name; label.className = 'check-field'; label.append(input, text); $('webhook-events').append(label);
    }
    $('webhook-note').textContent = data.managed
      ? 'Encaminhamento LylaHub protegido: URL do gateway, ativação, MESSAGES_UPSERT e ausência de base64 são mantidos. Os demais eventos podem ser editados.'
      : 'Configuração exclusiva desta instância. Não há webhook global nesta versão. Use HTTPS público sem segredos na URL. Alterar ou desativar interrompe a entrega ao destino anterior.';
    if (data.unsupported_events.length) $('webhook-note').textContent += ' Eventos sem efeito atualmente configurados serão removidos ao salvar: ' + data.unsupported_events.join(', ') + '.';
    if (data.sensitive_url) $('webhook-note').textContent += ' A URL atual contém dados sensíveis e foi ocultada. Informe um novo destino sem credenciais para salvar.';
    $('webhook-submit').disabled = webhookBusy;
  } catch (error) {
    if (generation === webhookGeneration && $('webhook-dialog').open) {
      showError('webhook-error', error.message); $('webhook-note').textContent = 'Feche e abra novamente para consultar a configuração.';
    }
  }
}
$('webhook-dialog').addEventListener('close', () => { webhookGeneration++; webhookInstance = null; webhookRevision = null; $('webhook-form').reset(); $('webhook-events').replaceChildren(); });
$('webhook-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!webhookInstance || !webhookRevision || webhookBusy || $('webhook-submit').disabled) return;
  const generation = webhookGeneration, identifier = webhookInstance.id;
  const body = {url: $('webhook-url').value, enabled: $('webhook-enabled').checked, base64: $('webhook-base64').checked,
    events: Array.from($('webhook-events').querySelectorAll('input')).filter(input => input.checked).map(input => input.value), revision: webhookRevision};
  webhookBusy = true; $('webhook-submit').disabled = true; $('webhook-fields').disabled = true; showError('webhook-error', '');
  try {
    await api('/instances/' + encodeURIComponent(identifier) + '/webhook', 'POST', body);
    if (generation === webhookGeneration && $('webhook-dialog').open) $('webhook-dialog').close();
    toast('Webhook salvo e configuração confirmada.');
  } catch (error) {
    if (generation === webhookGeneration && $('webhook-dialog').open) {
      showError('webhook-error', error.message);
      // After a timeout the write may have succeeded. Always reload before retrying.
      webhookRevision = null; $('webhook-note').textContent = 'Feche e abra novamente para consultar o estado antes de tentar salvar.';
    } else toast(error.message);
  } finally {
    webhookBusy = false;
    if ($('webhook-dialog').open && webhookRevision) { $('webhook-submit').disabled = false; $('webhook-fields').disabled = false; }
  }
});

let actionInstance = null, actionKind = null, actionBusy = false, actionGeneration = 0;
function openAction(instance, kind) {
  actionGeneration++; actionInstance = instance; actionKind = kind;
  const deleting = kind === 'delete';
  $('action-title').textContent = deleting ? 'Excluir instância' : 'Desconectar WhatsApp';
  $('action-instance').textContent = instance.name;
  $('action-effect').textContent = deleting
    ? 'Esta ação desconecta o WhatsApp e remove permanentemente a instância e sua configuração no WhatsMiau. Vínculos no backend Python não são apagados; precisam ser revisados separadamente. Para continuar, digite o nome exato abaixo.'
    : 'Esta ação encerra a sessão do WhatsApp e interrompe mensagens e eventos deste número. A instância e o webhook permanecem. Você poderá reconectar pelo QR code após a desconexão.';
  $('action-name-label').textContent = 'Digite: ' + instance.name;
  $('action-name').value = ''; $('action-name-field').hidden = !deleting;
  $('action-submit').textContent = deleting ? 'Excluir permanentemente' : 'Confirmar desconexão';
  $('action-submit').disabled = deleting || actionBusy;
  showError('action-error', ''); $('action-dialog').showModal();
  if (deleting) $('action-name').focus();
}
$('action-name').addEventListener('input', () => { $('action-submit').disabled = actionBusy || !actionInstance || $('action-name').value !== actionInstance.name; });
$('action-dialog').addEventListener('close', () => { actionGeneration++; actionInstance = null; actionKind = null; $('action-name').value = ''; });
$('action-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!actionInstance || actionBusy || (actionKind === 'delete' && $('action-name').value !== actionInstance.name)) return;
  const instance = actionInstance, kind = actionKind, generation = actionGeneration;
  actionBusy = true; $('action-submit').disabled = true; showError('action-error', '');
  try {
    await api('/instances/' + encodeURIComponent(instance.id) + '/' + kind, 'POST', {confirm: true, name: instance.name});
    if (generation === actionGeneration && $('action-dialog').open) $('action-dialog').close();
    if (kind === 'logout') {
      const current = instances.find(i => i.id === instance.id);
      if (current) { current.state = 'close'; current.number = ''; render(); }
    } else { instances = instances.filter(i => i.id !== instance.id); render(); }
    toast(kind === 'logout' ? 'WhatsApp desconectado. Use Conectar WhatsApp para obter um novo QR code.' : 'Instância excluída.');
    loadInstances();
  } catch (error) {
    if (generation === actionGeneration && $('action-dialog').open) showError('action-error', error.message + ' Atualize a lista antes de repetir a ação.');
    else toast(error.message);
    loadInstances();
  } finally {
    actionBusy = false;
    if ($('action-dialog').open && actionInstance) $('action-submit').disabled = actionKind === 'delete' && $('action-name').value !== actionInstance.name;
  }
});

let infoGeneration = 0, credentialGeneration = 0, credentialKey = null, credentialBusy = false, credentialTimer;
function clearCredential() {
  credentialGeneration++;
  credentialKey = null; clearTimeout(credentialTimer); $('credential-key').value = '••••••••••••••••';
  $('credential-copy').disabled = true; $('credential-reveal').textContent = 'Revelar chave global';
}
async function openInfo(instance) {
  const generation = ++infoGeneration; clearCredential();
  $('info-id').value = instance.id; $('info-name').value = instance.name; $('info-api').value = '';
  $('credential-reveal').disabled = true; showError('info-error', ''); $('info-dialog').showModal();
  try {
    const data = await api('/instances/' + encodeURIComponent(instance.id) + '/details');
    if (generation !== infoGeneration || !$('info-dialog').open) return;
    $('info-id').value = data.id; $('info-name').value = data.name; $('info-api').value = data.api_url;
    $('credential-reveal').disabled = credentialBusy;
  } catch (error) { if (generation === infoGeneration && $('info-dialog').open) showError('info-error', error.message); }
}
$('info-dialog').addEventListener('close', () => { infoGeneration++; clearCredential(); $('info-api').value = ''; });
$('credential-reveal').addEventListener('click', async () => {
  if (credentialKey) { clearCredential(); return; }
  if (credentialBusy || $('credential-reveal').disabled || !$('info-dialog').open) return;
  const generation = infoGeneration, secretGeneration = credentialGeneration;
  credentialBusy = true; $('credential-reveal').disabled = true; showError('info-error', '');
  try {
    const data = await api('/credentials/reveal', 'POST', {confirm: true});
    if (generation !== infoGeneration || secretGeneration !== credentialGeneration || !$('info-dialog').open || document.hidden) return;
    credentialKey = data.api_key; $('credential-key').value = credentialKey;
    $('credential-copy').disabled = false; $('credential-reveal').textContent = 'Ocultar chave';
    credentialTimer = setTimeout(clearCredential, 60000);
  } catch (error) { if (generation === infoGeneration && $('info-dialog').open) showError('info-error', error.message); }
  finally { credentialBusy = false; if ($('info-dialog').open) $('credential-reveal').disabled = false; }
});
$('credential-copy').addEventListener('click', async () => {
  if (!credentialKey || !$('info-dialog').open) return;
  try { await navigator.clipboard.writeText(credentialKey); toast('Chave global copiada. Ela permite acesso a todas as instâncias.'); }
  catch { showError('info-error', 'Não foi possível copiar. Selecione a chave revelada para copiar manualmente.'); }
});
document.addEventListener('visibilitychange', () => { if (document.hidden) clearCredential(); });
window.addEventListener('pagehide', clearCredential);
loadInstances();

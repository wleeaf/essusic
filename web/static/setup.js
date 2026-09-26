'use strict';
const $ = id => document.getElementById(id);
let csrf = '';
let busy = false;
function message(text, error = false) {
  $('message').textContent = text;
  $('message').classList.toggle('error', error);
  $('message').hidden = !text;
}
function lock(text) {
  $('workspace').hidden = true;
  $('locked').hidden = false;
  $('guild-name').textContent = 'Open setup from Discord';
  message(text, true);
}
async function api(path, method = 'GET', data) {
  const response = await fetch('/setup/' + path, {
    method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) lock(result.message);
    throw new Error(result.message || 'The request failed. Try again.');
  }
  return result;
}
function render(sources) {
  const labels = {missing: 'Not connected', configured: 'Saved · check connection', ready: 'Connection checked', attention: 'Needs attention'};
  for (const provider of ['youtube', 'spotify']) {
    const source = sources[provider];
    const badge = $(provider + '-state');
    badge.textContent = labels[source.state] || 'Needs attention';
    badge.dataset.state = source.state;
    $(provider + '-checked').textContent = source.checked_at
      ? 'Last checked ' + new Date(source.checked_at * 1000).toLocaleString() : 'No connection check yet';
    document.querySelector('[data-remove="' + provider + '"]').disabled = source.state === 'missing';
    document.querySelector('[data-test="' + provider + '"]').disabled = source.state === 'missing';
  }
  $('finish-title').textContent = sources.youtube.state === 'ready' ? 'Your server is ready for a first track.' : 'Connect. Check. Press play.';
}
async function refresh() {
  const result = await api('status');
  csrf = result.csrf;
  $('guild-name').textContent = result.guild.name;
  render(result.sources);
}
async function action(work) {
  if (busy) return;
  busy = true;
  const buttons = [...document.querySelectorAll('button')];
  const previous = buttons.map(button => button.disabled);
  buttons.forEach(button => { button.disabled = true; });
  message('Working on your server’s connection…');
  try { await work(); }
  catch (error) { message(error.message || 'The request failed. Try again.', true); }
  finally {
    buttons.forEach((button, index) => { button.disabled = previous[index]; });
    busy = false;
    if (!$('workspace').hidden) {
      try { await refresh(); } catch (error) { message(error.message, true); }
    }
  }
}
$('cookies').addEventListener('change', () => {
  $('file-name').textContent = $('cookies').files[0]?.name || 'No file selected';
});
$('youtube-form').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    const file = $('cookies').files[0];
    if (!file || file.size > 128 * 1024) throw new Error('Choose a cookie file smaller than 128 KiB.');
    await api('sources/youtube', 'PUT', {cookies: await file.text()});
    $('youtube-form').reset();
    $('file-name').textContent = 'No file selected';
    message('YouTube session saved. Test the connection to check playback access.');
  });
});
$('spotify-form').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    const credentials = {client_id: $('client-id').value.trim(), client_secret: $('client-secret').value.trim()};
    $('client-secret').value = '';
    await api('sources/spotify', 'PUT', credentials);
    $('spotify-form').reset();
    message('Spotify credentials saved. Test the connection to check API access.');
  });
});
document.querySelectorAll('[data-test]').forEach(button => button.addEventListener('click', () => action(async () => {
  const result = await api('sources/' + button.dataset.test + '/test', 'POST', {});
  message(result.message, !result.success);
})));
document.querySelectorAll('[data-remove]').forEach(button => button.addEventListener('click', () => action(async () => {
  await api('sources/' + button.dataset.remove, 'DELETE');
  message('Credentials removed from this server.');
})));
$('logout').addEventListener('click', () => action(async () => {
  await api('session', 'DELETE');
  $('youtube-form').reset(); $('spotify-form').reset();
  $('workspace').hidden = true;
  $('guild-name').textContent = 'Setup session closed';
  message('All done here. Head back to Discord and use /play in a voice channel.');
}));
(async () => {
  let token = window.location.hash.slice(1);
  history.replaceState(null, '', '/setup');
  try {
    if (token) await api('session', 'POST', {token});
    token = '';
    await refresh();
    $('workspace').hidden = false;
  } catch (error) { token = ''; lock(error.message); }
})();

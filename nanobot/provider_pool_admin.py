"""Server-rendered admin section for the provider pool.

Kept as a plain static string (no f-string) so the inline HTML/JS needs no brace
escaping. The section talks to the existing admin endpoints through
``window.nanobotAdminRequest`` (mutations) and ``fetch`` (the list read).
"""

from __future__ import annotations

PROVIDER_POOL_SECTION = """\
<section><h2>Provider pool &mdash; many keys, no rate limiting</h2>
<p class='hint'>Add up to 40 OpenAI-compatible lanes (base URL + API key + model). The agent rotates
across the enabled lanes and automatically fails over to the next lane when one returns a rate-limit,
overload, server or connection error &mdash; so a single key can never rate-limit the whole app.
Keys stay on the server and are only ever shown masked. Saving stores the pool in the Northflank service
environment &mdash; nothing is written to the database, so no Supabase egress is used.</p>
<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.5rem'>
<label>Base URL<input id='poolBaseUrl' type='url' placeholder='https://example.com/v1'></label>
<label>API key<input id='poolApiKey' type='password' placeholder='sk-...' autocomplete='off'></label>
<label>Model<input id='poolModel' placeholder='gpt-4o-mini'></label>
<label>Label (optional)<input id='poolLabel' placeholder='kyma-1'></label>
</div>
<button id='poolAdd'>Add entry</button>
<button id='poolReload' class='secondary'>Reload</button>
<button id='poolTestAll' class='secondary'>Test all enabled</button>
<p class='hint'>Pool: <strong id='poolCount'>0 / 40</strong></p>
<div style='overflow:auto;margin-top:1rem'><table><thead><tr>
<th>Label</th><th>Base URL</th><th>Key</th><th>Model</th><th>Enabled</th><th>Actions</th>
</tr></thead><tbody id='poolRows'><tr><td colspan='6'>Loading...</td></tr></tbody></table></div>
<pre id='poolResults' class='hint'>No tests run yet.</pre>
<p id='poolStatus' class='hint'></p></section>
<script>(function(){
  var $ = function(id){ return document.getElementById(id); };
  var setStatus = function(text, ok){
    var el = $('poolStatus');
    el.textContent = text;
    el.style.color = (ok === false) ? '#fca5a5' : '#a7f3d0';
  };
  var sync = function(v){
    var nf = v && v.northflank;
    if(!nf){ return; }
    if(nf.synced){ setStatus('Saved and stored in the Northflank environment.'); }
    else if(nf.configured){ setStatus('Saved here, but the Northflank sync failed: ' + (nf.error || 'unknown error'), false); }
    else { setStatus('Saved. Add a Northflank API token to persist it across restarts.'); }
  };
  var esc = function(value){
    return String(value == null ? '' : value).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  };
  var req = function(action, payload){ return window.nanobotAdminRequest(action, payload || {}); };
  var showResults = function(results){
    $('poolResults').textContent = JSON.stringify(results || [], null, 2);
  };
  var render = function(v){
    var rows = v.entries || [];
    var max = v.max || 40;
    $('poolCount').textContent = rows.length + ' / ' + max;
    var body = $('poolRows');
    if(!rows.length){
      body.innerHTML = "<tr><td colspan='6'>No pool entries yet.</td></tr>";
      return;
    }
    body.innerHTML = rows.map(function(e){
      var enabled = e.enabled ? ' checked' : '';
      return '<tr><td>' + esc(e.label) + '</td><td>' + esc(e.baseUrl) + '</td><td>' + esc(e.apiKeyMasked) + '</td><td>' + esc(e.model) + '</td>'
        + '<td><input type="checkbox" data-role="enabled" data-id="' + esc(e.id) + '"' + enabled + '></td>'
        + '<td><button class="secondary" data-role="test" data-id="' + esc(e.id) + '">Test</button>'
        + '<button class="secondary" data-role="delete" data-id="' + esc(e.id) + '">Delete</button></td></tr>';
    }).join('');
  };
  var load = function(){
    fetch('/api/admin/provider-pool', {cache:'no-store'})
      .then(function(r){ if(!r.ok){ throw new Error('Could not load the pool (' + r.status + ')'); } return r.json(); })
      .then(render)
      .catch(function(e){ setStatus(e.message, false); });
  };
  $('poolAdd').onclick = function(){
    var payload = {
      baseUrl: $('poolBaseUrl').value,
      apiKey: $('poolApiKey').value,
      model: $('poolModel').value,
      label: $('poolLabel').value
    };
    setStatus('Adding entry...');
    req('admin.provider.pool.add', payload).then(function(v){
      render(v);
      $('poolApiKey').value = '';
      $('poolModel').value = '';
      sync(v);
    }).catch(function(e){ setStatus(e.message, false); });
  };
  $('poolReload').onclick = load;
  $('poolTestAll').onclick = function(){
    setStatus('Testing enabled lanes...');
    req('admin.provider.pool.test', {all:true}).then(function(v){
      showResults(v.results);
      setStatus('Test complete.');
    }).catch(function(e){ setStatus(e.message, false); });
  };
  $('poolRows').addEventListener('click', function(ev){
    var button = ev.target.closest('button');
    if(!button){ return; }
    var id = button.getAttribute('data-id');
    var role = button.getAttribute('data-role');
    if(role === 'delete'){
      if(!confirm('Remove this pool entry?')){ return; }
      setStatus('Removing...');
      req('admin.provider.pool.delete', {id:id}).then(function(v){ render(v); sync(v); })
        .catch(function(e){ setStatus(e.message, false); });
    } else if(role === 'test'){
      setStatus('Testing lane...');
      req('admin.provider.pool.test', {id:id}).then(function(v){
        showResults(v.results);
        setStatus('Test complete.');
      }).catch(function(e){ setStatus(e.message, false); });
    }
  });
  $('poolRows').addEventListener('change', function(ev){
    var box = ev.target;
    if(box.getAttribute('data-role') !== 'enabled'){ return; }
    var id = box.getAttribute('data-id');
    setStatus('Saving...');
    req('admin.provider.pool.update', {id:id, enabled: box.checked}).then(function(v){ render(v); sync(v); })
      .catch(function(e){ setStatus(e.message, false); });
  });
  var init = function(){ load(); };
  if(document.readyState === 'loading'){ document.addEventListener('DOMContentLoaded', init); } else { init(); }
})();</script>
"""


def provider_pool_section() -> str:
    """Return the admin HTML/JS block for the provider pool."""
    return PROVIDER_POOL_SECTION

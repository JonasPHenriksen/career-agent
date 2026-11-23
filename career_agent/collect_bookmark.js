/* The Collect jobs bookmark. collect.bookmark() fills in __APP__ and __KEY__, drops this comment and joins the lines,
   so every statement below ends with a semicolon, and there are no line comments and no percent signs.

   One click takes the whole search, not just the page you're on:
   - Jobindex and IT-Jobbank keep a page's results in its own data (window.Stash). The bookmark reads every page of
     the search from your tab, the way your browser would show them, up to MAX_PAGES, a little apart.
   - A page with a "load more" button (Jobnet's "Indlæs flere job") has it pressed until everything is showing.
   - Any other page is sent as it is.
   Reading more pages takes a while, so a box on the page says how far it has got, and you press Send at the end:
   the browser only lets a page open the app's tab straight after a click. */
(function () {
  var APP = '__APP__', KEY = '__KEY__', MAX_PAGES = 50, MAX_MORE = 100, PAUSE = 600, d = document, box = null;
  if (location.origin === APP) { alert('Drag this button to your bookmarks bar. Then click it there while a job search results page is open.'); return; }
  function post(html, pages) {
    var f = d.createElement('form');
    f.method = 'post'; f.action = APP + '/collect/take'; f.target = '_blank'; f.acceptCharset = 'UTF-8'; f.style.display = 'none';
    [['token', KEY], ['url', location.href], ['html', html], ['pages', String(pages)]].forEach(function (p) {
      var i = d.createElement('input'); i.type = 'hidden'; i.name = p[0]; i.value = p[1]; f.appendChild(i);
    });
    (d.body || d.documentElement).appendChild(f); f.submit(); f.remove();
  }
  function say(text, button) {
    if (!box) {
      box = d.createElement('div');
      box.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:320px;padding:14px 16px;background:#fff;color:#111;border:1px solid #bbb;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.25);font:14px/1.4 -apple-system,system-ui,sans-serif';
      (d.body || d.documentElement).appendChild(box);
    }
    box.textContent = 'Career Agent: ' + text;
    if (button) {
      var b = d.createElement('button');
      b.textContent = button.label; b.style.cssText = 'display:block;margin-top:10px;padding:6px 12px;font:inherit;cursor:pointer';
      b.onclick = function () { box.remove(); box = null; button.go(); };
      box.appendChild(b);
    }
  }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function searchResponse(stash) {
    var found = null;
    (function walk(o) {
      if (found || !o || typeof o !== 'object') return;
      if (o.searchResponse && Array.isArray(o.searchResponse.results)) { found = o.searchResponse; return; }
      for (var k in o) walk(o[k]);
    })(stash);
    return found;
  }
  function pageUrl(n) {
    var u = new URL(location.href);
    if (n > 1) u.searchParams.set('page', String(n)); else u.searchParams.delete('page');
    return u.href;
  }
  async function readPage(n) {
    var t = await (await fetch(pageUrl(n), { credentials: 'include' })).text();
    var a = t.indexOf('var Stash = '), b = t.indexOf('</script>', a);
    if (a < 0 || b < 0) throw new Error('page ' + n + ' had no results');
    var data = t.slice(a + 12, b);
    var r = searchResponse(JSON.parse(data.slice(0, data.lastIndexOf('}') + 1)));
    if (!r) throw new Error('page ' + n + ' had no results');
    return r;
  }
  async function everyPage() {
    try {
      say('reading page 1 …');
      var first = await readPage(1), per = first.results.length || 20;
      var total = Math.min(Math.ceil(first.hitcount / per), MAX_PAGES), ads = first.results.map(function (r) { return r.html; });
      for (var p = 2; p <= total; p++) {
        say('reading page ' + p + ' of ' + total + ' …');
        await wait(PAUSE);
        ads = ads.concat((await readPage(p)).results.map(function (r) { return r.html; }));
      }
      var cut = first.hitcount > total * per ? ' The search has ' + first.hitcount + ' jobs; narrow it to get the rest.' : '';
      say(ads.length + ' jobs from ' + total + ' page' + (total === 1 ? '' : 's') + '.' + cut, {
        label: 'Send to Career Agent',
        go: function () { post('<html><body>' + ads.join('\n') + '</body></html>', total); }
      });
    } catch (e) {
      say('could not read every page (' + e.message + ').', { label: 'Send just this page', go: function () { post(d.documentElement.outerHTML, 1); } });
    }
  }
  function moreButton() {
    return Array.prototype.find.call(d.querySelectorAll('button'), function (b) {
      return /^(indlæs|vis) flere( job)?$|^(load|show) more( jobs)?$/i.test(b.textContent.trim()) && !b.disabled;
    });
  }
  async function loadAll() {
    var clicks = 0, more, before, w;
    while ((more = moreButton()) && clicks < MAX_MORE) {
      before = d.querySelectorAll('a[href]').length;
      more.click(); clicks += 1;
      say('loading more jobs … (' + clicks + ')');
      for (w = 0; w < 40 && d.querySelectorAll('a[href]').length <= before; w++) await wait(250);
      if (d.querySelectorAll('a[href]').length <= before) break;
      await wait(PAUSE);
    }
    say('all the jobs are showing.', { label: 'Send to Career Agent', go: function () { post(d.documentElement.outerHTML, 1); } });
  }
  var here = searchResponse(window.Stash);
  if (here && here.hitcount > here.results.length) { everyPage(); return; }
  if (moreButton()) { loadAll(); return; }
  post(d.documentElement.outerHTML, 1);
})();

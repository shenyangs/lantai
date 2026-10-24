// Private stdio bridge. No TCP listener, no user browser profile, no shell commands.
import {createInterface} from 'node:readline';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
const require = createRequire(path.join(process.argv[2], 'package.json'));
const protocolWrite = process.stdout.write.bind(process.stdout);
process.stdout.write = () => true;
const send = (value) => protocolWrite(JSON.stringify(value) + '\n');
const browsers = new Set();
const {chromium} = require('patchright');
const originalLaunch = chromium.launch.bind(chromium);
chromium.launch = async (options = {}) => {
  const browser = await originalLaunch({...options, chromiumSandbox: true,
    args: (options.args || []).filter(arg => !['--no-sandbox','--disable-setuid-sandbox','--ignore-certificate-errors','--ignore-certificate-errors-spki-list'].includes(arg))});
  const newContext = browser.newContext.bind(browser);
  browser.newContext = (options = {}) => newContext({...options, ignoreHTTPSErrors: false});
  browsers.add(browser);
  browser.on('disconnected', () => browsers.delete(browser));
  return browser;
};
const {init, request} = await import(pathToFileURL(path.join(process.argv[2], 'node_modules/rsshub/dist-lib/pkg.mjs')).href);
const {load} = require('cheerio');
let ready = false;
let configuration = {};
const escape = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
async function publicGithubTrending(path) {
  const parts = path.split('?')[0].split('/').filter(Boolean);
  const since = decodeURIComponent(parts[2] || 'daily');
  const language = decodeURIComponent(parts[3] || 'any');
  if (!['daily','weekly','monthly'].includes(since)) throw new Error('invalid trending range');
  const url = new URL('https://github.com/trending' + (language === 'any' ? '' : '/' + encodeURIComponent(language)));
  url.searchParams.set('since', since);
  if (parts[4]) url.searchParams.set('spoken_language_code', decodeURIComponent(parts[4]));
  const response = await fetch(url, {signal:AbortSignal.timeout(20000), headers:{'User-Agent':'Lantai-Reading/1.0'}});
  if (!response.ok) throw new Error('upstream fetch failed');
  let size = 0; const chunks = [];
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > 2 * 1024 * 1024) throw new Error('upstream response too large');
    chunks.push(Buffer.from(chunk));
  }
  const $ = load(Buffer.concat(chunks).toString('utf8'));
  const item = $('article.Box-row').toArray().flatMap(row => {
    const href = $(row).find('h2 a').attr('href');
    if (!href || !/^\/[\w.-]+\/[\w.-]+$/.test(href)) return [];
    return [{title:href.slice(1),link:'https://github.com'+href,description:'<p>'+escape($(row).find('p').text().trim())+'</p>'}];
  });
  if (!item.length) throw new Error('upstream page contained no trending repositories');
  return {title:`GitHub Trending · ${language} · ${since}`,link:url.href,item};
}
if (process.argv[3] === '--check') {
  await init({CACHE_TYPE:'memory'});
  send({ok:true,ready:true});
  process.exit(0);
}
let closing = false;
async function close() {
  if (closing) return;
  closing = true;
  await Promise.allSettled([...browsers].map(browser => browser.close()));
  process.exit(0);
}
process.on('SIGTERM', close);
process.on('SIGINT', close);
const lines = createInterface({input: process.stdin, crlfDelay: Infinity});
for await (const line of lines) {
  let command;
  try {
    command = JSON.parse(line);
    if (command.action === 'init') {
      configuration = command.config || {};
      await init({...command.config, CACHE_TYPE:'memory', CACHE_EXPIRE:300, REQUEST_TIMEOUT:15000});
      ready = true;
      send({id:command.id,ok:true,data:{ready:true}});
      continue;
    }
    if (!ready) throw new Error('not initialized');
    const data = command.path.startsWith('/github/trending/') && !configuration.GITHUB_ACCESS_TOKEN
      ? await publicGithubTrending(command.path) : await request(command.path);
    if (data?.error) throw new Error(String(data.error.message || data.error));
    if (!Array.isArray(data?.item)) throw new Error('invalid feed response');
    const payload = JSON.stringify(data);
    if (Buffer.byteLength(payload) > 8 * 1024 * 1024) throw new Error('response too large');
    send({id:command.id,ok:true,data});
  } catch(error) {
    const text = String(error?.message || error);
    const code = /executable.*exist|browserType.launch|install.*browser/i.test(text) ? 'browser_required'
      : /config|token|credential|login|cookie|unauthorized|401|403/i.test(text) ? 'configuration_required'
      : /timeout|fetch|ECONN|ENOTFOUND|socket|network/i.test(text) ? 'upstream_unavailable'
      : /not found|404/i.test(text) ? 'route_not_found' : 'route_failed';
    // Route exceptions can contain account URLs and cookies. Never return raw errors.
    send({id:command?.id,ok:false,error:code});
  }
}
await close();

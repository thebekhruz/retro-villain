import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// push.js — самовызывающийся скрипт страницы: гоняем его на заглушках браузера.
const SOURCE = readFileSync(new URL('../../retro/static/push.js', import.meta.url), 'utf8');

function fakeNode(tag) {
  return {
    tag, children: [], dataset: {}, textContent: '', className: '', disabled: false, listeners: {},
    setAttribute() {}, append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    querySelectorAll() { return this.children.filter(node => node.tag === 'button'); },
  };
}

function box(push) {
  const node = fakeNode('div');
  node.hidden = true;
  node.dataset.push = push;
  return node;
}

function buttons(node) {
  return node.children.find(child => child.className === 'push-actions')?.children.map(child => child.textContent) || [];
}

async function run({permission, subscribed}) {
  const boxes = [box('until-on'), box('')];
  const subscription = {
    endpoint: 'https://push.example/1', options: {}, toJSON: () => ({endpoint: 'https://push.example/1'}),
    unsubscribe: async () => true,
  };
  const registration = {pushManager: {
    getSubscription: async () => (subscribed ? subscription : null),
    subscribe: async () => subscription,
  }};
  const Notification = {permission, requestPermission: async () => 'granted'};
  const sandbox = {
    document: {querySelectorAll: () => boxes, createElement: fakeNode},
    navigator: {userAgent: 'Mozilla/5.0 (X11; Linux x86_64) Chrome/130', platform: 'Linux', maxTouchPoints: 0,
      serviceWorker: {register: async () => registration}},
    window: {isSecureContext: true, PushManager: function PushManager() {}, Notification},
    Notification,
    matchMedia: () => ({matches: false}),
    fetch: async () => ({ok: true, json: async () => ({key: 'AQAB'})}),
    atob: text => Buffer.from(text, 'base64').toString('binary'),
  };
  new Function(...Object.keys(sandbox), SOURCE)(...Object.values(sandbox));
  for (let i = 0; i < 20; i += 1) await new Promise(resolve => setImmediate(resolve));
  return boxes;
}

test('выключено — плашка «Включить» видна везде', async () => {
  const [chef, profile] = await run({permission: 'default', subscribed: false});
  assert.equal(chef.hidden, false);
  assert.equal(profile.hidden, false);
  assert.deepEqual(buttons(chef), ['Включить']);
});

test('включено — «Проверить» и «Выключить» в Счёте Шефа и в профиле', async () => {
  const [chef, profile] = await run({permission: 'granted', subscribed: true});
  assert.equal(chef.dataset.state, 'on');
  assert.deepEqual(buttons(chef), ['Проверить', 'Выключить']);
  assert.deepEqual(buttons(profile), ['Проверить', 'Выключить']);
  const title = node => node.children[0].children[0].textContent;
  assert.equal(title(chef), 'Уведомления');  // строка в самом блоке Счёта Шефа
  assert.equal(title(profile), 'Уведомления о Счёте Шефа');
});

test('включено — в Счёте Шефа тонкая строка на компьютере и ничего на телефоне', () => {
  const css = readFileSync(new URL('../../retro/static/founder-cabinet.css', import.meta.url), 'utf8');
  const on = '.push-box[data-push="until-on"][data-state="on"]';
  assert.match(css, new RegExp(on.replace(/[[\]().]/g, '\\$&') + '\\{[^}]*border:0;background:none'));
  assert.ok(css.includes('@media(max-width:900px){' + on + '{display:none}}'));
});

test('нажали «Включить» — плашка в Счёте Шефа становится строкой «включены»', async () => {
  const [chef, profile] = await run({permission: 'default', subscribed: false});
  chef.children.find(child => child.className === 'push-actions').children[0].listeners.click();
  for (let i = 0; i < 20; i += 1) await new Promise(resolve => setImmediate(resolve));
  assert.equal(chef.dataset.state, 'on');
  assert.deepEqual(buttons(chef), ['Проверить', 'Выключить']);
  assert.equal(profile.dataset.state, 'on');
});

test('запрещены в браузере — плашка возвращается с подсказкой', async () => {
  const [chef] = await run({permission: 'denied', subscribed: true});
  assert.equal(chef.hidden, false);
  assert.equal(chef.dataset.state, 'denied');
});

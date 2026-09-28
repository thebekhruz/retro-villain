import test from 'node:test';
import assert from 'node:assert/strict';
import '../../retro/static/founder-broadcast-logic.js';

const logic=globalThis.FounderBroadcastLogic;

test('broadcast draft must be non-empty and within Telegram text limit',()=>{
  assert.equal(logic.validateText('   ').ok,false);
  assert.equal(logic.validateText('x'.repeat(4097)).ok,false);
  assert.deepEqual(logic.validateText('  Новое меню  '),{ok:true,text:'Новое меню'});
});

test('broadcast operation ids are UUIDs and terminal states stop polling',()=>{
  assert.match(logic.operationId({randomUUID:()=> '12345678-1234-4123-8123-123456789012'}),/^[0-9a-f-]{36}$/);
  assert.equal(logic.isTerminal('running'),false);
  assert.equal(logic.isTerminal('completed'),true);
  assert.match(logic.statusText({status:'completed',sent:2,blocked:1,failed:0,audience:3}),/отправлено 2/);
});

test('targeted broadcast requires unique recipients that still exist',()=>{
  const available=['A'.repeat(32),'B'.repeat(32)];
  assert.equal(logic.validateRecipients([],available).ok,false);
  assert.equal(logic.validateRecipients(['A'.repeat(32),'missing'],available).ok,false);
  assert.deepEqual(logic.validateRecipients(['B'.repeat(32),'B'.repeat(32)],available),{
    ok:true,ids:['B'.repeat(32)],
  });
});

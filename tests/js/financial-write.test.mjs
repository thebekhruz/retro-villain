import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

test('financial writes preserve key across lost response and release after successful replay', async () => {
  const values = new Map(), keys = [];
  let attempts = 0, sequence = 0;
  const context = {window:{}, crypto:{randomUUID:()=>String(++sequence)}, sessionStorage:{
    getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
    fetch:async (url, options)=>{
      keys.push(options.headers['Idempotency-Key']); attempts++;
      return {ok:true,status:201,clone:()=>({arrayBuffer:async()=>{
        if(attempts===1) throw new Error('Connection lost while reading response');
        return new ArrayBuffer(0);
      }})};
    }};
  vm.runInNewContext(fs.readFileSync('retro/static/financial-write.js','utf8'),context);
  const write=()=>context.window.RetroFinancialWrite('/api/cashier/expenses',{method:'POST',body:'{"amount":"10"}'});
  await assert.rejects(write(),/Connection lost/);
  await write();
  assert.deepEqual(keys,['1','1']);
  await write();
  assert.deepEqual(keys,['1','1','2']);
});

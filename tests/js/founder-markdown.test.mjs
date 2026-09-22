import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../../retro/static/founder-markdown.js',import.meta.url),'utf8');
const context={};vm.createContext(context);vm.runInContext(source,context);
const {parse}=context.FounderMarkdown;

test('parses assistant tables, headings and bold text',()=>{
  const blocks=parse(`### 1. Продажи в убыток\n\n| Позиция | Выручка | Убыток |\n|---|---:|---:|\n| **RED BULL** | 312 000 | −305 004 |\n\n> ⚠ Эти позиции продаются **ниже себестоимости**`);
  assert.equal(blocks[0].type,'heading');
  assert.equal(blocks[1].type,'table');
  assert.equal(blocks[1].rows.length,1);
  assert.equal(blocks[1].rows[0][0][0].type,'strong');
  assert.equal(blocks[1].alignments[1],'right');
  assert.equal(blocks[2].type,'quote');
  assert.equal(blocks[2].content[1].type,'strong');
});

test('does not treat raw html as markup',()=>{
  const blocks=parse('<img src=x onerror=alert(1)> **важно**');
  assert.equal(blocks[0].type,'paragraph');
  assert.equal(blocks[0].content[0].value,'<img src=x onerror=alert(1)> ');
  assert.equal(blocks[0].content[1].type,'strong');
});

test('keeps escaped pipes inside a table cell',()=>{
  const blocks=parse('| Поле | Значение |\n|---|---|\n| A \\| B | 10 |');
  assert.equal(blocks[0].rows[0].length,2);
  assert.equal(blocks[0].rows[0][0][0].value,'A | B');
});

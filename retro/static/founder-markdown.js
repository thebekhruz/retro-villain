((root)=>{
  function splitTableRow(line){
    const value=line.trim().replace(/^\|/,'').replace(/\|$/,'');
    const cells=[];let cell='',escaped=false;
    for(const char of value){
      if(escaped){cell+=char;escaped=false;continue}
      if(char==='\\'){escaped=true;continue}
      if(char==='|'){cells.push(cell.trim());cell='';continue}
      cell+=char;
    }
    cells.push(cell.trim());return cells;
  }

  function isDivider(line){
    const cells=splitTableRow(line);
    return cells.length>1&&cells.every(cell=>/^:?-{3,}:?$/.test(cell.replace(/\s/g,'')));
  }

  function inline(value){
    const tokens=[];const pattern=/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g;
    let cursor=0,match;
    while((match=pattern.exec(value))){
      if(match.index>cursor)tokens.push({type:'text',value:value.slice(cursor,match.index)});
      const token=match[0];
      if(token.startsWith('**'))tokens.push({type:'strong',value:token.slice(2,-2)});
      else if(token.startsWith('`'))tokens.push({type:'code',value:token.slice(1,-1)});
      else{const parts=token.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);tokens.push({type:'link',value:parts[1],href:parts[2]})}
      cursor=pattern.lastIndex;
    }
    if(cursor<value.length)tokens.push({type:'text',value:value.slice(cursor)});
    return tokens;
  }

  function parse(markdown){
    const lines=String(markdown||'').replace(/\r\n?/g,'\n').split('\n');
    const blocks=[];
    for(let index=0;index<lines.length;){
      const line=lines[index];
      if(!line.trim()){index+=1;continue}
      const heading=line.match(/^(#{1,6})\s+(.+)$/);
      if(heading){blocks.push({type:'heading',level:heading[1].length,content:inline(heading[2].trim())});index+=1;continue}
      if(index+1<lines.length&&line.includes('|')&&isDivider(lines[index+1])){
        const headers=splitTableRow(line),alignments=splitTableRow(lines[index+1]).map(cell=>{
          const clean=cell.replace(/\s/g,'');return clean.startsWith(':')&&clean.endsWith(':')?'center':clean.endsWith(':')?'right':clean.startsWith(':')?'left':null;
        });
        const rows=[];index+=2;
        while(index<lines.length&&lines[index].includes('|')&&lines[index].trim()){
          const cells=splitTableRow(lines[index]);
          while(cells.length<headers.length)cells.push('');
          rows.push(cells.slice(0,headers.length).map(inline));index+=1;
        }
        blocks.push({type:'table',headers:headers.map(inline),alignments,rows});continue;
      }
      const list=line.match(/^\s*(?:[-*]|\d+[.)])\s+(.+)$/);
      if(list){
        const ordered=/^\s*\d+[.)]/.test(line),items=[];
        while(index<lines.length){const item=lines[index].match(/^\s*(?:[-*]|\d+[.)])\s+(.+)$/);if(!item||(/^\s*\d+[.)]/.test(lines[index]))!==ordered)break;items.push(inline(item[1]));index+=1}
        blocks.push({type:'list',ordered,items});continue;
      }
      if(/^>\s?/.test(line)){const parts=[];while(index<lines.length&&/^>\s?/.test(lines[index])){parts.push(lines[index].replace(/^>\s?/,''));index+=1}blocks.push({type:'quote',content:inline(parts.join(' '))});continue}
      const paragraph=[line.trim()];index+=1;
      while(index<lines.length&&lines[index].trim()&&!/^(#{1,6})\s+/.test(lines[index])&&!/^\s*(?:[-*]|\d+[.)])\s+/.test(lines[index])&&!/^>\s?/.test(lines[index])&&!(index+1<lines.length&&lines[index].includes('|')&&isDivider(lines[index+1]))){paragraph.push(lines[index].trim());index+=1}
      blocks.push({type:'paragraph',content:inline(paragraph.join(' '))});
    }
    return blocks;
  }

  function appendInline(document,target,tokens){
    tokens.forEach(token=>{
      if(token.type==='text'){target.append(document.createTextNode(token.value));return}
      const node=document.createElement(token.type==='link'?'a':token.type);
      node.textContent=token.value;
      if(token.type==='link'){node.href=token.href;node.target='_blank';node.rel='noopener noreferrer'}
      target.append(node);
    });
  }

  function render(document,markdown){
    const rootNode=document.createElement('div');rootNode.className='ai-chat-content';
    const blocks=parse(markdown);
    blocks.forEach(block=>{
      if(block.type==='table'){
        const wrap=document.createElement('div');wrap.className='ai-chat-table-wrap';wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','Таблица в ответе помощника');
        const table=document.createElement('table'),head=document.createElement('thead'),headerRow=document.createElement('tr');
        if(block.headers.length<=4)table.className='is-compact';
        block.headers.forEach((content,index)=>{const th=document.createElement('th');th.scope='col';if(block.alignments[index])th.style.textAlign=block.alignments[index];appendInline(document,th,content);headerRow.append(th)});
        head.append(headerRow);table.append(head);const body=document.createElement('tbody');
        block.rows.forEach(row=>{const tr=document.createElement('tr');row.forEach((content,index)=>{const td=document.createElement('td');if(block.alignments[index])td.style.textAlign=block.alignments[index];appendInline(document,td,content);tr.append(td)});body.append(tr)});
        table.append(body);wrap.append(table);rootNode.append(wrap);return;
      }
      const tag=block.type==='heading'?`h${Math.min(block.level+2,6)}`:block.type==='list'?(block.ordered?'ol':'ul'):block.type==='quote'?'blockquote':'p';
      const node=document.createElement(tag);
      if(block.type==='list')block.items.forEach(content=>{const item=document.createElement('li');appendInline(document,item,content);node.append(item)});
      else appendInline(document,node,block.content);
      rootNode.append(node);
    });
    return {node:rootNode,hasTable:blocks.some(block=>block.type==='table')};
  }

  root.FounderMarkdown={parse,render};
})(typeof window==='undefined'?globalThis:window);

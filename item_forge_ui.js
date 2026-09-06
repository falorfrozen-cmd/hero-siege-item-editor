// Item Forge: one shared workspace for new items and every owned-item entry point.
const ITEMFORGE_RARITIES=[['','Original rarity'],['1','Common'],['3','Rare'],['5','Legendary'],['6','Satanic'],['7','Angelic'],['9','Heroic'],['10','Unholy']];
let ITEM_FORGE_CAT=null,FORGE_SESSION=null,charHost=null,charPick=null;
const FORGE_ICONS={
  forge:'M14 4l6 6M12 6l6 6M13 7L4 16l4 4 9-9M16 3l5 5-3 3-5-5z',
  plus:'M12 5v14M5 12h14',arrow:'M5 12h14M13 6l6 6-6 6',back:'M19 12H5M11 6l-6 6 6 6',
  box:'M3 7l9-4 9 4v10l-9 4-9-4zM3 7l9 4 9-4M12 11v10',
  sliders:'M4 6h16M4 12h16M4 18h16M8 4v4M16 10v4M10 16v4',
  spark:'M12 3l2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6z',
  shield:'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z',
  sword:'M14 3h7v7L9 22l-7-7L14 3zM5 12l7 7M15 9l3-3',
  book:'M3 5c4-1 6 0 9 2 3-2 5-3 9-2v15c-4-1-6 0-9 1-3-1-5-2-9-1zM12 7v14',
  bolt:'M13 2L4 14h7l-1 8L21 9h-8z',
  check:'M5 12l4 4L19 6',close:'M6 6l12 12M18 6L6 18',
  eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  trash:'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7',
};
function forgeIcon(name){return `<svg class="f-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${FORGE_ICONS[name]||FORGE_ICONS.spark}"/></svg>`}
function forgeImage(item){return item&&item.spr?`<img class="f-item-image" src="/icons/${attr(item.spr)}.png?v=2" alt="" loading="lazy">`:`<span class="f-item-image">${forgeIcon('box')}</span>`}
function forgeLabel(row){return row.advanced?`Unknown property #${row.key}`:row.label||`Property #${row.key}`}
function forgeCategory(row){
  const l=(row.label||'').toLowerCase();
  if(/fire|cold|lightning|arcane|poison|element|shadow|holy|frost|burn/.test(l))return 'elements';
  if(/skill|talent|haste|cooldown|mana cost/.test(l)||row.pickerKind)return 'skills';
  if(/life|defense|armor|resist|block|dodge|mitigation|replenish|absorb|taken|heal|regen|thorn|return/.test(l))return 'defense';
  if(/damage|attack|crit|cast|speed|deadly|crushing|projectile|rating|weapon|blow|ailment|bleed|pierce|range/.test(l))return 'offense';
  return 'utility';
}
const FORGE_CATEGORIES=[['all','All properties','sliders'],['offense','Offense','sword'],['defense','Defense','shield'],['skills','Skills & procs','book'],['elements','Elements','bolt'],['utility','Utility','spark']];
function forgeRuntimeBanner(rt){
  if(!rt)return '';
  const level=rt.level==='ok'?'ok':rt.level==='danger'?'danger':'warn';
  const title=level==='ok'?'ForgePact ready':level==='danger'?'ForgePact setup needed':'ForgePact · check before playing';
  return `<details class="f-runtime ${level}"><summary><span class="f-dot"></span>${title}</summary><div>${esc(rt.message||'Status unavailable.')}${rt.gameDir?`<code>${esc(rt.gameDir)}</code>`:''}</div></details>`;
}
function closeForgePicker(){const modal=document.getElementById('sockmodal');if(modal&&modal.forgeClose)modal.forgeClose();else if(modal)modal.remove();charHost=null;charPick=null}
function forgeDialog(title,subtitle,body,footer){
  closeForgePicker();
  const previousFocus=document.activeElement,modal=document.createElement('div');modal.id='sockmodal';
  modal.innerHTML=`<div id="sockbox" class="forge-dialog" role="dialog" aria-modal="true" aria-labelledby="forge-dialog-title"><header class="f-dialog-head"><div><div class="f-eyebrow">Item Forge</div><h3 id="forge-dialog-title">${esc(title)}</h3><p>${esc(subtitle)}</p></div><button class="f-icon-btn" data-dialog-close aria-label="Close dialog">${forgeIcon('close')}</button></header><div class="f-dialog-body">${body}</div><footer class="f-dialog-foot">${footer}</footer></div>`;
  const roots=['left','mid','right','topbar'].map(id=>document.getElementById(id)).filter(Boolean),priorInert=roots.map(el=>el.inert);
  roots.forEach(el=>el.inert=true);
  let closed=false;
  const dialog={modal,q:selector=>modal.querySelector(selector),onclose:null,close(){
    if(closed)return;closed=true;modal.remove();roots.forEach((el,i)=>el.inert=priorInert[i]);
    if(previousFocus&&previousFocus.isConnected)previousFocus.focus();if(dialog.onclose)dialog.onclose();
  }};
  modal.forgeClose=dialog.close;modal.querySelector('[data-dialog-close]').onclick=dialog.close;
  modal.onclick=event=>{if(event.target===modal)dialog.close()};
  modal.onkeydown=event=>{
    if(event.key==='Escape'){event.preventDefault();dialog.close();return}
    if(event.key==='Tab'){
      const focusable=[...modal.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),summary,[tabindex="0"]')].filter(el=>el.getClientRects().length);
      const first=focusable[0],last=focusable[focusable.length-1];
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
    }
  };
  document.body.appendChild(modal);dialog.q('[data-dialog-close]').focus();return dialog;
}
function forgePageControls(page,total,size){return `<div class="f-pagination"><button class="f-btn ghost" data-page-prev ${page===0?'disabled':''}>${forgeIcon('back')} Previous</button><span>Page ${page+1} of ${Math.max(1,Math.ceil(total/size))}</span><button class="f-btn ghost" data-page-next ${(page+1)*size>=total?'disabled':''}>Next ${forgeIcon('arrow')}</button></div>`}

function mountForgeEditor(host,db,configuration){
  const statRows=db.stats||[],statMap=new Map(statRows.map(row=>[String(row.key),row]));
  const selected=new Map(),selectedPresetIds=new Set(),excludedKeys=new Set();let undo=null;
  let base=new Map(),removedBase=new Set(),baseSource='';   // the item's own stats (key -> original value) and the ones the user removed
  const baseRow=(key,value)=>({...recordFor(key,value),base:true,baseValue:Number(value)});
  const pickers=db.pickers||{talents:[],classes:[]};
  const pickerOptions=row=>row.pickerKind==='talent'?pickers.talents||[]:pickers.classes||[];
  const familyFor=key=>{const keys=statMap.get(String(key))?.linkedKeys;return [...new Set((keys?.length?keys:[+key]).map(Number))].sort((a,b)=>a-b)};
  const recordFor=(key,value)=>({...statMap.get(String(key)),key:+key,label:statMap.has(String(key))?forgeLabel(statMap.get(String(key))):`Unknown property #${key}`,value:value==null?NaN:Number(value)});
  const presetMap=new Map();for(const donor of db.donors||[])for(const property of donor.properties||[])presetMap.set(property.id,{donor,property});
  const snapshot=()=>{const explicit=removedBase.size>0||!keep.checked;const stats={};for(const [key,row] of selected){if(row.base&&!explicit&&row.value===row.baseValue)continue;stats[key]=row.value}return {stats,presetIds:[...selectedPresetIds],excludedKeys:[...excludedKeys],keepNative:!explicit}};
  const notify=()=>{if(host.onForgeChange)host.onForgeChange()};
  const optionLabel=(row,value)=>{const choice=pickerOptions(row).find(option=>Number(option.id)===value);return choice?`${choice.label}${choice.className?' · '+choice.className:''}`:'Choose '+(row.pickerKind==='talent'?'skill':'class')};
  function groups(){
    const map=new Map();for(const row of selected.values()){const id=familyFor(row.key).join('-');if(!map.has(id))map.set(id,[]);map.get(id).push(row)}
    return [...map].map(([id,rows])=>({id,rows:rows.sort((a,b)=>a.key-b.key),label:groupLabel(rows)})).sort((a,b)=>a.label.localeCompare(b.label));
  }
  function groupLabel(rows){const lead=rows.find(row=>row.pickerKind==='talent')||rows[0];return lead.label.replace(/\s*\((?:Skill|Talent|Class)\s*ID\)/ig,'').replace(/\s+(?:Skill|Talent)\s*ID$/i,'').replace(/:\s*Skill$/i,'')}
  host.innerHTML=`<div class="f-section-head"><div><h3>Item properties <span class="f-count" data-fe-count></span></h3><p>Add a stat, then set the value you want.</p></div><button class="f-btn" data-fe-add>${forgeIcon('plus')} Add property</button></div><div data-fe-selected></div><div class="f-undo" data-fe-undo hidden><span>Property removed.</span><button class="f-btn ghost" data-fe-restore>Undo</button></div><div class="f-native"><label><input type="checkbox" data-fe-keep checked> Keep other original stats</label><p data-fe-native-note>Custom values replace matching stats. Other original stats stay on the item.</p></div>`;
  const q=selector=>host.querySelector(selector),chosen=q('[data-fe-selected]'),keep=q('[data-fe-keep]');
  function renderSelected(){
    const all=groups();q('[data-fe-count]').textContent=all.length?`· ${all.length}`:'';
    chosen.innerHTML=all.length?all.map(group=>`<section class="f-property" data-forge-group="${attr(group.id)}"><div class="f-property-head"><span class="f-category-mark">${forgeIcon(FORGE_CATEGORIES.find(cat=>cat[0]===forgeCategory(group.rows[0]))?.[2])}</span><div><b>${esc(group.label)}</b>${group.rows.length>1?'<small>Linked effect · edited together</small>':''}${group.rows.some(row=>row.base)?`<small class="f-base-tag">Base stat · original ${esc(group.rows.filter(row=>row.base).map(row=>row.baseValue.toLocaleString('en-US',{maximumFractionDigits:2})).join(' / '))}</small>`:''}</div><button class="f-icon-btn" data-fe-remove="${attr(group.id)}" aria-label="Remove ${attr(group.label)}">${forgeIcon('trash')}</button></div><div class="f-fields">${group.rows.map(row=>`<div class="f-field-row" data-custom-key="${row.key}"><label for="fe-value-${row.key}">${esc(group.rows.length===1?'Value':row.label.replace(/\s*\(.*?ID\)/g,''))}</label>${row.pickerKind?`<button id="fe-value-${row.key}" class="f-btn f-skill-choice ${Number.isFinite(row.value)?'':'needs-choice'}" data-picker="${row.key}" type="button" aria-label="${attr(row.label)} choice">${esc(optionLabel(row,row.value))}${forgeIcon('arrow')}</button>`:`<div class="f-value"><input id="fe-value-${row.key}" data-fe-value="${row.key}" type="number" step="any" value="${Number.isFinite(row.value)?attr(String(row.value)):''}" aria-label="${attr(row.label)} value" aria-invalid="${!Number.isFinite(row.value)}" ${row.safeEditable?'':'readonly'}><span>${esc(row.unit||'')}</span></div>`}</div>`).join('')}</div><details class="f-stat-detail"><summary>About this property${group.rows.some(row=>row.confidence==='code-heuristic')?' · estimated meaning':''}</summary>${group.rows.map(row=>`<p>${esc(row.plainDescription||row.evidenceNote||'The name is verified from the game code; no explanation has been written for it yet.')} <span>Property #${row.key}${row.safeEditable===false&&!row.pickerKind?' · fixed by its item or socket seed':''}</span></p>`).join('')}</details></section>`).join(''):`<div class="f-empty">${forgeIcon('sliders')}<b>Make it your own</b><p>Choose from stats, skills and unique item properties. Set each value right here.</p><button class="f-btn primary" data-fe-empty-add>${forgeIcon('plus')} Add your first property</button></div>`;
    chosen.querySelectorAll('[data-fe-value]').forEach(input=>input.oninput=()=>{selected.get(input.dataset.feValue).value=input.value.trim()===''?NaN:Number(input.value);input.setAttribute('aria-invalid',String(!Number.isFinite(selected.get(input.dataset.feValue).value)));notify()});
    chosen.querySelectorAll('[data-picker]').forEach(button=>button.onclick=()=>openValuePicker(selected.get(button.dataset.picker)));
    chosen.querySelectorAll('[data-fe-remove]').forEach(button=>button.onclick=()=>{
      undo=snapshot();const members=button.dataset.feRemove.split('-');if(members.includes('201')&&selected.has('21')&&!selected.get('21')?.base)members.push('21');
      for(const member of members){if(selected.get(member)?.base)removedBase.add(member);selected.delete(member);excludedKeys.add(member)}
      if(removedBase.size){keep.checked=false;keep.disabled=true;updateNative()}
      renderSelected();q('[data-fe-undo]').hidden=false;notify();
    });
    if(q('[data-fe-empty-add]'))q('[data-fe-empty-add]').onclick=openLibrary;
  }
  function load(cfg){
    selected.clear();selectedPresetIds.clear();excludedKeys.clear();
    for(const [key,value] of Object.entries(cfg?.stats||{}))selected.set(String(key),recordFor(key,value));
    (cfg?.presetIds||[]).forEach(id=>selectedPresetIds.add(id));(cfg?.excludedKeys||[]).forEach(key=>excludedKeys.add(String(key)));keep.checked=cfg?.keepNative!==false;
    removedBase.clear();keep.disabled=false;
    for(const [key,value] of base){const row=selected.get(key);if(row){row.base=true;row.baseValue=Number(value)}else if(cfg&&cfg.keepNative===false)removedBase.add(key);else selected.set(key,baseRow(key,value))}
    if(removedBase.size){keep.checked=false;keep.disabled=true}
    renderSelected();updateNative();q('[data-fe-undo]').hidden=true;
  }
  function updateNative(){const hint=baseSource==='model'?' Exact base stats (rolled affixes included) appear once this item has been loaded in Hero Siege with ForgePact.':'';q('[data-fe-native-note]').dataset.hint=hint;if(removedBase.size){q('[data-fe-native-note]').textContent='A base stat was removed: the item now carries exactly the properties listed here (the remaining base stats are written out explicitly).';return}q('[data-fe-native-note]').textContent=keep.checked?'Custom values replace matching stats. Other original stats stay on the item.':'Only the custom properties listed here will remain. Original item stats are removed.'}
  keep.onchange=()=>{updateNative()+(q('[data-fe-native-note]').dataset.hint||'');notify()};
  q('[data-fe-restore]').onclick=()=>{if(undo){load(undo);undo=null;notify()}};
  function openValuePicker(row){
    const options=pickerOptions(row),classes=[...new Set(options.map(option=>option.className).filter(Boolean))].sort();let page=0;
    const d=forgeDialog(row.pickerKind==='talent'?'Choose a skill':'Choose a class',row.label,`<div class="f-picker-toolbar"><input class="f-search" data-choice-search placeholder="Search ${row.pickerKind==='talent'?'skills':'classes'}…" aria-label="Search choices">${classes.length?`<select data-choice-class aria-label="Filter skill class"><option value="">All classes</option>${classes.map(name=>`<option>${esc(name)}</option>`).join('')}</select>`:''}</div><div data-choice-results></div>`,`<span>Choose an entry to use it on your item.</span><button class="f-btn" data-choice-cancel>Cancel</button>`);
    function render(){
      const query=d.q('[data-choice-search]').value.trim().toLowerCase(),cls=d.q('[data-choice-class]')?.value;
      const hits=options.filter(option=>(!cls||option.className===cls)&&(!query||`${option.label} ${option.className||''}`.toLowerCase().includes(query)));
      d.q('[data-choice-results]').innerHTML=`<div class="f-choice-list">${hits.slice(page*35,(page+1)*35).map(option=>`<button data-choice="${option.id}">${esc(option.label)}<small>${esc(option.className||'')}${Number(option.id)===row.value?' · Selected':''}</small></button>`).join('')||'<div class="f-empty">No matching choices.</div>'}</div>${forgePageControls(page,hits.length,35)}`;
      d.modal.querySelectorAll('[data-choice]').forEach(button=>button.onclick=()=>{row.value=Number(button.dataset.choice);d.close();renderSelected();notify();q(`[data-picker="${row.key}"]`)?.focus()});
      d.q('[data-page-prev]').onclick=()=>{page--;render()};d.q('[data-page-next]').onclick=()=>{page++;render()};
    }
    d.q('[data-choice-search]').oninput=()=>{page=0;render()};if(d.q('[data-choice-class]'))d.q('[data-choice-class]').onchange=()=>{page=0;render()};d.q('[data-choice-cancel]').onclick=d.close;render();d.q('[data-choice-search]').focus();
  }
  function addSafeFamily(row){
    const family=familyFor(row.key),blocked=family.map(key=>statMap.get(String(key))).find(meta=>!meta||(!meta.safeEditable&&!meta.pickerKind));
    if(blocked)return false;
    for(const key of family){if(!selected.has(String(key))){const meta=statMap.get(String(key));selected.set(String(key),recordFor(key,meta.pickerKind?null:meta.recommendedValue))}excludedKeys.delete(String(key))}
    // 'All Skills: Class' (#21) only tags 'to All Skills' (#201); alone the game draws nothing.
    if(family.includes(21)&&!selected.has('201')&&statMap.has('201')){const meta=statMap.get('201');selected.set('201',recordFor(201,meta.recommendedValue));excludedKeys.delete('201')}
    renderSelected();notify();return true;
  }
  function addPreset(id){
    const found=presetMap.get(id);if(!found)return false;
    const overlaps=Object.keys(found.property.stats||{}).filter(key=>selected.has(key)&&selected.get(key).value!==Number(found.property.stats[key]));
    if(overlaps.length&&!confirm(`This property will replace ${overlaps.length} value(s) already in your draft. Use the values from ${found.donor.name}?`))return false;
    selectedPresetIds.add(id);
    for(const [key,value] of Object.entries(found.property.stats||{})){selected.set(key,recordFor(key,value));familyFor(key).forEach(member=>excludedKeys.delete(String(member)))}
    for(const key of found.property.needsClass||[]){if(!selected.has(String(key)))selected.set(String(key),recordFor(key,null));excludedKeys.delete(String(key))}
    renderSelected();notify();return true;
  }
  function openLibrary(){
    let mode='stats',cat='all',page=0;const seen=new Set(),stats=[];
    // A proc's skill, level and chance are one choice, not three nearly identical results.
    for(const row of statRows){const id=familyFor(row.key).join('-');if(seen.has(id))continue;seen.add(id);const family=familyFor(row.key).map(key=>statMap.get(String(key))).filter(Boolean);const lead=family.find(meta=>meta.pickerKind==='talent')||row;stats.push({...lead,search:family.map(meta=>`${meta.label} ${meta.plainDescription||''} ${meta.key}`).join(' ').toLowerCase(),family})}
    stats.sort((a,b)=>forgeLabel(a).localeCompare(forgeLabel(b)));
    const properties=[...presetMap].map(([id,{donor,property}])=>({id,donor,property,search:`${donor.name} ${property.label} ${property.plainDescription||''}`.toLowerCase()}));
    const d=forgeDialog('Add properties','Choose a property. Fine-tune its values in your item.',`<div class="f-source-tabs"><button data-library-mode="stats" class="on">Stats & skills</button><button data-library-mode="unique">From unique items</button></div><div class="f-picker-toolbar"><input class="f-search" data-fe-search placeholder="Search life, damage, magic find…" aria-label="Search properties"></div><div class="f-library-layout"><nav class="f-library-nav" aria-label="Property categories">${FORGE_CATEGORIES.map(([id,label,icon])=>`<button data-fe-cat="${id}" class="${id==='all'?'on':''}">${forgeIcon(icon)}${label}</button>`).join('')}<label data-technical-label><input type="checkbox" data-fe-unknown> Technical properties</label></nav><div><div class="f-library-count" data-library-count></div><div data-fe-results></div><div data-library-pages></div></div></div>`,`<span data-library-added></span><button class="f-btn primary" data-library-done>Done ${forgeIcon('check')}</button>`);
    const search=d.q('[data-fe-search]'),showUnknown=d.q('[data-fe-unknown]');
    function render(){
      const query=search.value.trim().toLowerCase();let hits;
      if(mode==='stats')hits=stats.filter(row=>(!row.advanced||showUnknown.checked)&&(cat==='all'||forgeCategory(row)===cat)&&(!query||row.search.includes(query)));
      else hits=properties.filter(row=>(!query||row.search.includes(query))&&(cat==='all'||(row.property.keys||[]).some(key=>forgeCategory(statMap.get(String(key))||{})===cat)));
      const size=30;page=Math.min(page,Math.max(0,Math.ceil(hits.length/size)-1));
      d.q('[data-library-count]').textContent=`${hits.length.toLocaleString()} ${mode==='stats'?'properties':'unique properties'}${query?' matching your search':''}`;
      d.q('[data-fe-results]').innerHTML=hits.slice(page*size,(page+1)*size).map(row=>{
        if(mode==='stats'){
          const added=familyFor(row.key).every(key=>selected.has(String(key))),locked=row.family.some(meta=>!meta.safeEditable&&!meta.pickerKind);
          const note=locked?(familyFor(row.key).includes(20)?'Use Edit sockets from the item menu.':'Available through a verified unique property.'):row.plainDescription||'Set a custom value on your item.';
          return `<article class="f-library-result"><div><b>${esc(groupLabel(row.family))}</b><p>${esc(note)}</p>${row.family.length>1?'<small>Skill, level and related values stay together</small>':''}${row.confidence==='code-heuristic'?'<small>Estimated meaning</small>':''}</div><button class="f-btn ${added?'ghost':''}" data-custom-stat="${row.key}" ${locked||added?'disabled':''} aria-label="${added?'Added':locked?'Unavailable':'Add'} ${attr(groupLabel(row.family))}">${added?'Added':locked?'Fixed':'+ Add'}</button></article>`;
        }
        const added=selectedPresetIds.has(row.id)&&Object.keys(row.property.stats||{}).every(key=>selected.has(key)&&selected.get(key).value===Number(row.property.stats[key]));
        const donorItem=(ITEM_FORGE_CAT||[]).find(item=>Number(item.id)===Number(row.donor.catalogId));
        return `<article class="f-library-result">${forgeImage(donorItem)}<div><b>${esc(row.property.label)}</b><p>From ${esc(row.donor.name)}</p><details><summary>Property details</summary><p>${esc(row.property.plainDescription||'Copies this property with its linked effect values.')}</p></details></div><button class="f-btn ${added?'ghost':''}" data-custom-property="${attr(row.id)}" ${added?'disabled':''} aria-label="${added?'Added':'Add'} ${attr(row.property.label)} from ${attr(row.donor.name)}">${added?'Added':'+ Add'}</button></article>`;
      }).join('')||'<div class="f-empty"><b>No matching properties</b><p>Try another name or select All properties.</p></div>';
      d.q('[data-library-pages]').innerHTML=forgePageControls(page,hits.length,size);
      d.q('[data-page-prev]').onclick=()=>{page--;render();d.q('.f-dialog-body').scrollTop=0};d.q('[data-page-next]').onclick=()=>{page++;render();d.q('.f-dialog-body').scrollTop=0};
      d.modal.querySelectorAll('[data-custom-stat]').forEach(button=>button.onclick=()=>{if(addSafeFamily(statMap.get(button.dataset.customStat)))render()});
      d.modal.querySelectorAll('[data-custom-property]').forEach(button=>button.onclick=()=>{if(addPreset(button.dataset.customProperty))render()});
      d.q('[data-library-added]').textContent=`${groups().length} ${groups().length===1?'property':'properties'} on your item`;
    }
    d.modal.querySelectorAll('[data-library-mode]').forEach(button=>button.onclick=()=>{mode=button.dataset.libraryMode;page=0;search.placeholder=mode==='stats'?'Search life, damage, magic find…':'Search a unique item or its property…';d.modal.querySelectorAll('[data-library-mode]').forEach(other=>other.classList.toggle('on',other===button));d.q('[data-technical-label]').hidden=mode!=='stats';render();search.focus()});
    d.modal.querySelectorAll('[data-fe-cat]').forEach(button=>button.onclick=()=>{cat=button.dataset.feCat;page=0;d.modal.querySelectorAll('[data-fe-cat]').forEach(other=>other.classList.toggle('on',other===button));render()});
    search.oninput=()=>{page=0;render()};showUnknown.onchange=()=>{page=0;render()};d.q('[data-library-done]').onclick=d.close;
    d.onclose=()=>{const first=host.querySelector('.needs-choice,input[aria-invalid="true"]');if(first)first.focus()};render();search.focus();
  }
  q('[data-fe-add]').onclick=openLibrary;
  load(configuration);
  return {
    setConfiguration(cfg){load(cfg);notify()},setBase(obj,source){base=new Map(Object.entries(obj||{}).map(([key,value])=>[String(key),Number(value)]));baseSource=source||''},snapshot,count:()=>selected.size,groups,
    state(){for(const row of selected.values())if(!Number.isFinite(row.value))return {err:`${row.label}: ${row.pickerKind?'choose a '+(row.pickerKind==='talent'?'skill':'class'):'enter a number'}.`,key:row.key};return snapshot()},
    summary(){return [...selected.values()].map(row=>({label:row.label,value:row.pickerKind?optionLabel(row,row.value):Number.isFinite(row.value)?`${row.value.toLocaleString('en-US',{maximumFractionDigits:8})}${row.unit?' '+row.unit:''}`:'Value needed'}))},
    focusInvalid(key){host.querySelector(`#fe-value-${key}`)?.focus()}
  };
}

// Both pickers only choose an item. Creating a new item is deferred until Save.
function openForgeCatalogPicker(onSelected){
  const rows=(ITEM_FORGE_CAT||[]).filter(row=>['normal','unique'].includes(row.kind)&&row.cls>=0&&row.cls<=10&&row.available!==false);
  const types=[...new Set(rows.map(row=>row.clsName||CLS[row.cls]).filter(Boolean))].sort(),rarities=[...new Set(rows.map(row=>row.rar).filter(Boolean))];
  const d=forgeDialog('Choose your base item','Start from a normal base or a unique item. Your choice is a draft until you save.',`<div class="f-picker-toolbar"><input id="fpcq" class="f-search" placeholder="Search the item catalog…" aria-label="Search base items"><select id="fpctype" aria-label="Item type"><option value="">All types</option>${types.map(type=>`<option>${esc(type)}</option>`).join('')}</select><select id="fpcrar" aria-label="Item rarity"><option value="">All rarities</option>${rarities.map(rar=>`<option>${esc(rar)}</option>`).join('')}</select><select id="fpckind" aria-label="Base item kind"><option value="">All bases</option><option value="normal">Normal bases</option><option value="unique">Unique items</option></select></div><div class="f-library-count" data-catalog-count></div><div class="f-item-grid" id="fpcgrid"></div><div data-catalog-pages></div>`,`<span id="fpcinfo">Select an item to continue.</span><button class="f-btn primary" id="fpcgo" disabled>Use this base ${forgeIcon('arrow')}</button>`);
  let selectedRow=null,page=0;
  function choose(){if(!selectedRow)return;d.close();onSelected(selectedRow)}
  function render(){
    const query=d.q('#fpcq').value.trim().toLowerCase(),type=d.q('#fpctype').value,rar=d.q('#fpcrar').value,kind=d.q('#fpckind').value;
    const hits=rows.filter(row=>(!query||row.name.toLowerCase().includes(query))&&(!type||(row.clsName||CLS[row.cls])===type)&&(!rar||row.rar===rar)&&(!kind||row.kind===kind));
    d.q('[data-catalog-count]').textContent=`${hits.length.toLocaleString()} items`;
    d.q('#fpcgrid').innerHTML=hits.slice(page*48,(page+1)*48).map(row=>`<button class="f-item-tile" data-cid="${row.id}" aria-pressed="${selectedRow?.id===row.id}">${forgeImage(row)}<b class="r-${attr(row.rar||'_')}">${esc(row.name)}</b><small>${esc(row.clsName||CLS[row.cls]||'Item')}${row.kind==='unique'?' · Unique':''}</small></button>`).join('')||'<div class="f-empty">No matching items. Try another search.</div>';
    d.q('[data-catalog-pages]').innerHTML=forgePageControls(page,hits.length,48);
    d.q('[data-page-prev]').onclick=()=>{page--;render();d.q('.f-dialog-body').scrollTop=0};d.q('[data-page-next]').onclick=()=>{page++;render();d.q('.f-dialog-body').scrollTop=0};
    d.modal.querySelectorAll('[data-cid]').forEach(button=>{button.onclick=()=>{selectedRow=rows.find(row=>String(row.id)===button.dataset.cid);d.modal.querySelectorAll('[data-cid]').forEach(other=>other.setAttribute('aria-pressed',String(other===button)));d.q('#fpcgo').disabled=false;d.q('#fpcinfo').textContent=selectedRow.name};button.ondblclick=choose});
  }
  d.q('#fpcgo').onclick=choose;['#fpcq','#fpctype','#fpcrar','#fpckind'].forEach(selector=>{const input=d.q(selector);input.oninput=()=>{page=0;render()};if(input.tagName==='SELECT')input.onchange=input.oninput});render();d.q('#fpcq').focus();
}
function openForgeSignaturePicker(onPick){
  const d=forgeDialog('Forge a signature item','Ready-made items with their ForgePact mechanics. Pick one, adjust anything you like, then press FORGE ITEM.',`<div class="f-signature-list" data-signature-list><p role="status">Loading…</p></div>`,'');
  (async()=>{
    let rows=[];try{rows=(await j('/api/item-forge/signatures')).items||[]}catch(error){rows=[]}
    if(!ITEM_FORGE_CAT){try{ITEM_FORGE_CAT=await j('/api/catalog')}catch(error){ITEM_FORGE_CAT=[]}}
    const host=d.q('[data-signature-list]');if(!host)return;
    const usable=rows.map(sig=>({sig,row:(ITEM_FORGE_CAT||[]).find(r=>r.kind==='normal'&&r.key===sig.base.key&&(sig.base.cls==null||r.cls===sig.base.cls))})).filter(x=>x.row);
    host.innerHTML=usable.length?usable.map(({sig,row},index)=>`<button class="f-signature" data-signature-index="${index}">${forgeImage(row)}<div><strong>${esc(sig.name)}</strong><small>${esc(sig.base.name)} base · ${esc(sig.tagline)}</small><em>${esc(sig.needs)}</em></div><span class="f-choice-link">Use ${forgeIcon('arrow')}</span></button>`).join(''):'<div class="f-empty">No signature items are available (hs_signature_items.json missing or its bases are not in the catalog).</div>';
    host.querySelectorAll('[data-signature-index]').forEach(button=>button.onclick=()=>{const {sig,row}=usable[+button.dataset.signatureIndex];d.close();onPick(sig,row)});
  })();
}
function openForgeOwnedPicker(onPick){
  let source='character',rows=[],page=0,selection=null,token=0,remoteTotal=0,searchTimer=null;
  const d=forgeDialog('Choose an item you own','Browse your character, Shared Stash or Infinite Vault.',`<div class="f-source-tabs">${[['character','Character'],['stash','Shared Stash'],['vault','Infinite Vault']].map(([key,label])=>`<button data-owned-source="${key}" class="${key===source?'on':''}">${label}</button>`).join('')}</div><div class="f-picker-toolbar"><select data-owned-char aria-label="Character">${chars.map(char=>`<option value="${attr(char.slot)}">${esc(char.name)} · ${esc(char.cls)} · Lv. ${char.level}</option>`).join('')}</select><input class="f-search" data-owned-search placeholder="Search your items…" aria-label="Search owned items"><select data-owned-area aria-label="Item location"><option value="">All locations</option></select></div><div class="f-library-count" data-owned-count></div><div class="f-item-grid" data-owned-grid></div><div data-owned-pages></div>`,`<span data-owned-info>Select an item to continue.</span><button class="f-btn primary" data-owned-use disabled>Edit this item ${forgeIcon('arrow')}</button>`);
  function select(){if(!selection)return;d.close();onPick(selection.ref,selection.where,selection.item)}
  function render(){
    const query=d.q('[data-owned-search]').value.trim().toLowerCase(),area=d.q('[data-owned-area]').value;
    const hits=source==='vault'?rows:rows.filter(row=>(!query||`${row.item.name} ${row.item.customName||''}`.toLowerCase().includes(query))&&(!area||row.where===area));
    d.q('[data-owned-count]').textContent=`${(source==='vault'?remoteTotal:hits.length).toLocaleString()} items`;
    const visible=source==='vault'?hits:hits.slice(page*48,(page+1)*48);
    d.q('[data-owned-grid]').innerHTML=visible.map(row=>`<button class="f-item-tile" data-owned-index="${rows.indexOf(row)}" aria-pressed="${row===selection}">${row.item.customForge?.active?'<span class="f-forged">Forged</span>':''}${forgeImage(row.item)}<b class="r-${attr(row.item.rar||'_')}">${esc(row.item.customForge?.name||row.item.customName||row.item.name)}</b><small>${row.item.customForge?.name?esc(row.item.name)+' · ':''}${esc(row.where)}</small></button>`).join('')||'<div class="f-empty">No items found in this location.</div>';
    d.q('[data-owned-pages]').innerHTML=forgePageControls(page,source==='vault'?remoteTotal:hits.length,48);
    d.q('[data-page-prev]').onclick=()=>{page--;source==='vault'?load(false):render()};d.q('[data-page-next]').onclick=()=>{page++;source==='vault'?load(false):render()};
    d.modal.querySelectorAll('[data-owned-index]').forEach(button=>{button.onclick=()=>{selection=rows[+button.dataset.ownedIndex];d.modal.querySelectorAll('[data-owned-index]').forEach(other=>other.setAttribute('aria-pressed',String(other===button)));d.q('[data-owned-use]').disabled=false;d.q('[data-owned-info]').textContent=`${selection.item.name} · ${selection.where}`};button.ondblclick=select});
  }
  async function load(resetPage=true){
    const request=++token;selection=null;if(resetPage)page=0;rows=[];d.q('[data-owned-use]').disabled=true;d.q('[data-owned-info]').textContent='Select an item to continue.';d.q('[data-owned-grid]').innerHTML='<p role="status">Loading items…</p>';d.q('[data-owned-pages]').innerHTML='';d.q('[data-owned-char]').hidden=source!=='character';d.q('[data-owned-area]').hidden=source==='vault';
    try{
      if(source==='character'){
        if(!chars.length){render();return}
        const slot=Number(d.q('[data-owned-char]').value),data=await j('/api/char/'+slot);if(request!==token||!d.modal.isConnected)return;if(data.err)throw Error(data.err);
        const add=(items,target,where)=>(items||[]).forEach(item=>rows.push({item,ref:{target,key:item.key},where}));
        add(data.equipped,{type:'equipped',slot,tab:'equipped_items'},'Equipped');add(data.personal_stash,{type:'personal_stash',slot,tab:'personal_stash'},'Personal Stash');add(data.potions,{type:'potions',slot,tab:'potions'},'Potions');
        Object.entries(data.bags||{}).forEach(([tab,items])=>add(items,{type:'bag',slot,tab},BAG_LABELS[tab]||tab));
      }else if(source==='stash'){
        const data=await j('/api/stash');if(request!==token||!d.modal.isConnected)return;if(data.err)throw Error(data.err);
        Object.entries(data).forEach(([tab,items])=>{if(Array.isArray(items))items.forEach(item=>rows.push({item,ref:{target:{type:'stash',tab},key:item.key},where:tab==='unique_items'?'Unique items':tab.replace(/_/g,' ').replace(/^stash tab/,'Stash tab')}))});
      }else{
        const data=await j('/api/vault/items?collectionId=all&limit=48&offset='+(page*48)+'&q='+encodeURIComponent(d.q('[data-owned-search]').value.trim()));if(request!==token||!d.modal.isConnected)return;if(data.err)throw Error(data.err);
        remoteTotal=data.total||0;
        (data.items||[]).forEach(item=>rows.push({item,ref:{vaultItemId:item.id},where:item.collectionName||'Infinite Vault'}));
      }
      rows.sort((a,b)=>a.item.name.localeCompare(b.item.name));d.q('[data-owned-area]').innerHTML='<option value="">All locations</option>'+[...new Set(rows.map(row=>row.where))].sort().map(where=>`<option>${esc(where)}</option>`).join('');render();
    }catch(error){if(request===token&&d.modal.isConnected)d.q('[data-owned-grid]').innerHTML=`<div class="f-alert error">${esc(error.message||'Items could not be loaded.')}<br><button class="f-btn" data-owned-retry>Retry</button></div>`;d.q('[data-owned-retry]')?.addEventListener('click',load)}
  }
  d.modal.querySelectorAll('[data-owned-source]').forEach(button=>button.onclick=()=>{source=button.dataset.ownedSource;d.modal.querySelectorAll('[data-owned-source]').forEach(other=>other.classList.toggle('on',other===button));load()});
  d.q('[data-owned-char]').onchange=()=>load();d.q('[data-owned-search]').oninput=()=>{page=0;if(source==='vault'){clearTimeout(searchTimer);++token;d.q('[data-owned-use]').disabled=true;selection=null;searchTimer=setTimeout(()=>{if(d.modal.isConnected)load()},180)}else render()};d.q('[data-owned-area]').onchange=()=>{page=0;render()};d.q('[data-owned-use]').onclick=select;d.onclose=()=>{clearTimeout(searchTimer);++token};load();
}

async function openCustomForge(target,key,vaultRow=null){return openItemForge({ref:vaultRow?{vaultItemId:vaultRow.id}:{target,key},label:vaultRow?'Infinite Vault':'Owned item'})}
async function openItemForge(preset){
  if(!preset?.ref&&FORGE_SESSION?.app.isConnected)return;
  if(FORGE_SESSION?.dirty()&&!confirm('Discard the unsaved Item Forge draft?'))return;
  closeForgePicker();view='itemforge';curChar=null;gridReg={};gridSeq=0;
  document.querySelectorAll('.charbtn').forEach(button=>button.classList.remove('sel'));document.querySelectorAll('.tabbtn').forEach(button=>button.classList.toggle('sel',button.dataset.view==='itemforge'));
  const md=document.getElementById('mid');md.innerHTML='<div id="forge-app"><div class="f-eyebrow">Your item workshop</div><h2>Item Forge</h2><p role="status">Preparing your workshop…</p></div>';
  const app=md.firstElementChild;let busy=false,reference=null,catalogItem=null,current=null,baseItem=null,location='',step=1,tab='properties',baseline='',created=false,creationUncertain=false,saved=false,editor=null;
  const live=()=>app.isConnected;
  try{
    const data=await Promise.all([CUSTOM_FORGE_DB?Promise.resolve(CUSTOM_FORGE_DB):j('/api/custom-forge/catalog'),ITEM_FORGE_CAT?Promise.resolve(ITEM_FORGE_CAT):j('/api/catalog')]);if(!live())return;
    if(data[0].err||!Array.isArray(data[0].stats)||!Array.isArray(data[1]))throw Error('The item or property catalog is unavailable.');
    CUSTOM_FORGE_DB=data[0];ITEM_FORGE_CAT=data[1];
  }catch(error){if(live()){app.innerHTML=`<h2>Item Forge</h2><div class="f-alert error">${esc(error.message)}</div><button class="f-btn" data-forge-retry>Try again</button>`;app.querySelector('[data-forge-retry]').onclick=()=>{FORGE_SESSION=null;openItemForge()}}return}
  app.innerHTML=`<header class="f-heading"><div><div class="f-eyebrow">Your item workshop</div><h2>Item Forge</h2><p>Choose an item. Shape its properties. Make it yours.</p></div><span class="f-badge" id="ifdraftbadge">New workshop</span></header>
    <nav class="f-steps" aria-label="Forge progress"><button data-forge-step="1" aria-current="step"><span>1</span>Choose item</button><button data-forge-step="2" disabled><span>2</span>Customize</button><button data-forge-step="3" disabled><span>3</span>Review & save</button></nav>
    <section class="f-choose" data-forge-stage="1"><div class="f-eyebrow">Start with a base</div><h3>What would you like to forge?</h3><p>A fresh creation or a new chapter for an item you already own.</p><div class="f-choices"><button class="f-choice" data-if-tab="new"><span class="f-choice-icon">${forgeIcon('forge')}</span><strong>Create a new item</strong><small>Browse the item catalog and build on the base you like.</small><span class="f-choice-link">Browse catalog ${forgeIcon('arrow')}</span></button><button class="f-choice" data-if-tab="owned"><span class="f-choice-icon">${forgeIcon('box')}</span><strong>Customize an owned item</strong><small>Choose from your character, Shared Stash or Infinite Vault.</small><span class="f-choice-link">Choose an item ${forgeIcon('arrow')}</span></button><button class="f-choice" data-if-tab="signature"><span class="f-choice-icon">${forgeIcon('spark')}</span><strong>Forge a signature item</strong><small>Ready-made ForgePact items such as Headhunter and Tyrant's Crown, complete with their mechanics.</small><span class="f-choice-link">Pick a signature ${forgeIcon('arrow')}</span></button></div><div class="f-start-note">${forgeIcon('shield')}<span>Browse and experiment freely. Changes are written only when you save.</span></div><div id="ifstart-runtime"></div></section>
    <div class="f-workspace" id="ifworkspace" hidden><div class="f-main"><div class="f-card f-base" id="ifselected"></div>
      <section class="f-card" data-forge-stage="2"><nav class="f-tabs" role="tablist" aria-label="Item customization"><button id="iftab-properties" data-edit-tab="properties" role="tab" aria-controls="ifpanel-properties" aria-selected="true">${forgeIcon('sliders')}Properties</button><button id="iftab-appearance" data-edit-tab="appearance" role="tab" aria-controls="ifpanel-appearance" aria-selected="false">${forgeIcon('eye')}Appearance</button><button id="iftab-effects" data-edit-tab="effects" role="tab" aria-controls="ifpanel-effects" aria-selected="false">${forgeIcon('spark')}Special effects</button></nav>
      <div id="ifpanel-properties" data-edit-panel="properties" role="tabpanel" aria-labelledby="iftab-properties"><div id="ifeditor"></div></div>
      <div id="ifpanel-appearance" data-edit-panel="appearance" role="tabpanel" aria-labelledby="iftab-appearance" hidden><div class="f-section-head"><div><h3>A name and a story</h3><p>Optional. Leave a field empty to keep the original appearance.</p></div></div><div class="f-form"><label><span class="f-label-head">Custom name <small id="ifnamecount">0 / 48</small></span><input id="ifname" maxlength="48" placeholder="Keep original item name" autocomplete="off"></label><label>Rarity color<select id="ifrarity">${ITEMFORGE_RARITIES.map(([value,label])=>`<option value="${value}">${esc(label)}</option>`).join('')}</select></label><label class="wide"><span class="f-label-head">Special text <small id="ifaffixcount">0 / 240</small></span><textarea id="ifaffix" maxlength="240" rows="3" placeholder="A short golden inscription…"></textarea><small>Up to 3 lines above the stats. This text does not add an effect.</small></label><label class="wide"><span class="f-label-head">Lore <small id="iflorecount">0 / 1000</small></span><textarea id="iflore" maxlength="1000" rows="3" placeholder="Tell the story behind your item…"></textarea><small>Flavor text below the stats.</small></label></div></div>
      <div id="ifpanel-effects" data-edit-panel="effects" role="tabpanel" aria-labelledby="iftab-effects" hidden><div class="f-section-head"><div><h3>A special ability</h3><p>Optional. Choose one behavior for this item.</p></div></div><select id="ifmech" aria-label="Special mechanic" hidden><option value="">None</option><option value="headhunter">Headhunter</option><option value="tyrant">Tyrant's Crown</option></select>${[['','No extra ability','Use only the properties you selected.'],['headhunter','Headhunter','Defeating a rare monster grants its affixes as temporary buffs.'],['tyrant',"Tyrant’s Crown",'Nearby monsters become rare more often. Rare monsters gain one extra affix.']].map(([value,name,desc])=>`<label class="f-mechanic"><input type="radio" name="forge-mechanic" value="${value}" ${!value?'checked':''}><span><b>${esc(name)}${value==='headhunter'?'<em>Preview</em>':''}</b><small>${esc(desc)}</small></span></label>`).join('')}<p class="f-preview-note">These abilities require ForgePact 1.3.8 or later and its World switch enabled.</p></div></section>
      <section class="f-card" data-forge-stage="3" hidden><div class="f-section-head"><div><h3>Ready to forge?</h3><p>Check your item and its destination before saving.</p></div><button class="f-btn ghost" id="ifeditback">Edit</button></div><div id="ifreview"></div><div class="f-form" id="ifdestination"><label class="wide">Save new item to<select id="iftab">${Array.from({length:19},(_,i)=>`<option value="stash_tab_${i+1}">Shared Stash · Tab ${i+1}</option>`).join('')}</select></label></div><div class="f-review-check">${forgeIcon('shield')}<span>An automatic backup is made when existing data changes.</span></div><div class="f-review-check">${forgeIcon('check')}<span>Start Hero Siege with ForgePact after saving to load these changes.</span></div><details class="f-stat-detail"><summary>About identical copies</summary><p>Exact duplicate items with the same original definition receive the same forged setup.</p></details></section><div id="ifmessage" role="status" aria-live="polite"></div></div>
      <aside class="f-preview-column"><div class="f-preview-label"><span>Item preview</span><span>Custom values</span></div><div id="ifpreview" class="f-preview"></div><p class="f-preview-note">Draft summary, not an exact in-game tooltip. Original stats and game scaling are not calculated here.</p><div id="ifruntime"></div></aside></div>
    <footer class="f-footer" id="iffooter" hidden><div class="f-save-status" id="ifstatus">Unsaved draft<small>No changes written yet.</small></div><button class="f-btn ghost" id="ifremove" hidden>Remove forge</button><button class="f-btn ghost" id="ifreset" hidden>Discard changes</button><button class="f-btn" id="ifback">${forgeIcon('back')} Back</button><button class="f-btn primary" id="ifnext">Review item ${forgeIcon('arrow')}</button><button class="f-btn primary" id="ifgo" hidden>${forgeIcon('forge')} Save forged item</button></footer>`;
  const q=id=>app.querySelector('#'+id),message=(text,kind='error')=>{q('ifmessage').innerHTML=text?`<div class="f-alert ${kind}">${esc(text)}</div>`:'';if(text)q('ifmessage').scrollIntoView({block:'nearest',behavior:'instant'})};
  // Feedback stays visible even when a lookup fails on the first step.
  app.querySelector('.f-steps').after(q('ifmessage'));
  const identity=()=>({name:q('ifname').value.trim(),rarity:q('ifrarity').value,lore:q('iflore').value.trim(),affix:q('ifaffix').value.trim(),mechanic:q('ifmech').value});
  const fingerprint=()=>JSON.stringify({...identity(),...editor.snapshot(),destination:q('iftab').value});
  const dirty=()=>live()&&!!baseItem&&(busy||(!saved&&catalogItem!==null)||fingerprint()!==baseline);
  const session={app,dirty,isBusy:()=>busy};FORGE_SESSION=session;
  function setTab(next){tab=next;app.querySelectorAll('[data-edit-tab]').forEach(button=>{const active=button.dataset.editTab===tab;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1});app.querySelectorAll('[data-edit-panel]').forEach(panel=>panel.hidden=panel.dataset.editPanel!==tab)}
  function setStep(next){
    if(next>1&&!baseItem)return;step=next;q('ifworkspace').hidden=step===1;q('iffooter').hidden=step===1;
    app.querySelectorAll('[data-forge-stage]').forEach(panel=>panel.hidden=Number(panel.dataset.forgeStage)!==step);
    app.querySelectorAll('[data-forge-step]').forEach(button=>{button.disabled=busy||(!baseItem&&Number(button.dataset.forgeStep)>1);if(Number(button.dataset.forgeStep)===step)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current')});
    q('ifnext').hidden=step!==2;q('ifgo').hidden=step!==3;update();md.scrollTop=0;
  }
  function update(){
    if(!editor||!live())return;
    const id=identity(),valid=editor.state(),hasStats=editor.count()>0,changed=dirty();
    q('ifdraftbadge').textContent=baseItem?saved&&!changed?'Saved item':catalogItem&&!created?'New item draft':'Editing item':'New workshop';
    q('ifnamecount').textContent=`${q('ifname').value.length} / 48`;q('ifaffixcount').textContent=`${q('ifaffix').value.length} / 240`;q('iflorecount').textContent=`${q('iflore').value.length} / 1000`;
    q('ifgo').disabled=busy||GAME_RUNNING||creationUncertain||!hasStats||!!valid.err||!baseItem;
    q('ifnext').disabled=busy||!baseItem;q('ifback').disabled=busy;q('ifreset').disabled=busy;q('ifremove').disabled=busy||GAME_RUNNING;
    q('ifremove').hidden=!current?.configuration;q('ifreset').hidden=!baseItem||!changed||!!catalogItem;
    const status=GAME_RUNNING?'Game running · saving locked':busy?'Saving your item…':saved&&!changed?'All changes saved':created?'Base item created · forge not saved':changed?'Unsaved draft':'No changes yet';
    q('ifstatus').innerHTML=`${esc(status)}<small>${GAME_RUNNING?'Close Hero Siege to save.':saved&&!changed?'Start the game with ForgePact to apply it.':created?'Retry Save to finish this same item.':'Review your changes before saving.'}</small>`;
    if(!baseItem)return;
    const rarity=ITEMFORGE_RARITIES.find(row=>row[0]===id.rarity)?.[1],title=id.name||baseItem.name,summary=editor.summary();
    const colors={Common:'#cfcfcf',Normal:'#cfcfcf',Rare:'#ffd84d',Legendary:'#ff9c40',Satanic:'#ff7777',Angelic:'#ffe080',Heroic:'#78df98',Unholy:'#cba1ff'};
    q('ifpreview').style.setProperty('--f-item-color',colors[id.rarity?rarity:baseItem.rar]||'#efbd69');
    q('ifpreview').innerHTML=`${forgeImage(baseItem)}<h3>${esc(title)}</h3><div class="f-preview-type">${esc(id.rarity?rarity:baseItem.rar||'Original rarity')} · ${esc(baseItem.clsName||CLS[baseItem.cls]||'Item')}</div>${id.affix?`<div class="f-preview-affix">${esc(id.affix)}</div>`:''}<div class="f-preview-stats">${summary.length?summary.map(row=>`<div class="f-preview-stat"><span>${esc(row.label)}</span><b>${esc(row.value)}</b></div>`).join(''):'<p class="f-preview-note">Your custom properties will appear here.</p>'}</div>${id.mechanic?`<div class="f-preview-affix">${esc(id.mechanic==='headhunter'?'Headhunter':"Tyrant’s Crown")}</div>`:''}${id.lore?`<div class="f-preview-lore">${esc(id.lore)}</div>`:''}<div class="f-preview-foot">${editor.snapshot().keepNative?'Other original stats are kept.':'Original stats will be replaced.'}</div>`;
    const destination=catalogItem&&!created?(catalogItem.kind==='unique'?'Shared Stash · Unique items':q('iftab').selectedOptions[0].textContent):location||'Current item location';
    q('ifreview').innerHTML=`<dl class="f-review-list"><div><dt>Item</dt><dd>${esc(title)}</dd></div><div><dt>Action</dt><dd>${catalogItem&&!created?'Create a new item':'Update this item'}</dd></div><div><dt>Destination</dt><dd>${esc(destination)}</dd></div><div><dt>Custom properties</dt><dd>${editor.groups().length}</dd></div><div><dt>Other original stats</dt><dd>${editor.snapshot().keepNative?'Keep':'Remove'}</dd></div>${id.mechanic?`<div><dt>Ability</dt><dd>${id.mechanic==='headhunter'?'Headhunter':'Tyrant’s Crown'}</dd></div>`:''}</dl>${!hasStats?'<div class="f-alert">Add at least one property. Appearance changes also need a stat to apply in game.</div>':valid.err?`<div class="f-alert">${esc(valid.err)}</div>`:''}`;
    q('ifdestination').hidden=!catalogItem||created||catalogItem.kind==='unique';
  }
  editor=mountForgeEditor(q('ifeditor'),CUSTOM_FORGE_DB,null);q('ifeditor').onForgeChange=()=>{message('');update()};session.update=update;
  function fill(cfg){
    editor.setConfiguration(cfg);q('ifname').value=cfg?.name||'';q('ifrarity').value=cfg?.rarity!=null?String(cfg.rarity):'';q('iflore').value=cfg?.lore||'';q('ifaffix').value=cfg?.affix||'';q('ifmech').value=cfg?.mechanic||'';app.querySelectorAll('[name=forge-mechanic]').forEach(radio=>radio.checked=radio.value===q('ifmech').value);
    baseline=fingerprint();update();
  }
  function showBase(){const forgedName=current?.configuration?.name||baseItem.signature||'';q('ifselected').innerHTML=`${forgeImage(baseItem)}<div><strong class="r-${attr(baseItem.rar||'_')}">${esc(forgedName||baseItem.name)}</strong><small>${forgedName?esc(baseItem.name)+' · ':''}${esc(location)}${current?.configuration?' · Previously forged':''}</small>${Number(baseItem.cls)===15?`<div class="f-alert" data-socketable-note>Rune, gem or jewel: forged stats show on this item itself, but the game does not carry them into a socket. When it is socketed, the bonus is rebuilt from the item's type and seed. To change what it gives in a socket, use <b>Reroll stats</b> on it instead.</div>`:''}</div><button class="f-btn ghost" id="ifchange">Change</button>`;q('ifchange').onclick=()=>setStep(1)}
  async function selectReference(ref,label,item){
    const ownRequest={};session.lookup=ownRequest;busy=true;update();message('');
    try{
      const result=await j('/api/custom-forge',{method:'POST',body:JSON.stringify({action:'get',...ref})});if(!live()||session.lookup!==ownRequest)return;if(result.err)throw Error(result.err);
      reference=ref;catalogItem=null;created=false;creationUncertain=false;current=result;baseItem={...item,...result.item};location=label||'Owned item';saved=!!result.configuration;GAME_RUNNING=!!result.gameRunning;
      editor.setBase(result.baseStats||{},result.baseSource||'');fill(result.configuration);showBase();if(result.runtime)q('ifruntime').innerHTML=forgeRuntimeBanner(result.runtime);setTab('properties');setStep(2);
    }catch(error){if(live())message(error.message||'Could not load the item.')}finally{busy=false;update()}
  }
  const canChange=()=>!dirty()||confirm('Discard this unsaved draft and choose another item?');
  app.querySelectorAll('[data-if-tab]').forEach(button=>button.onclick=()=>{
    if(busy||!canChange())return;
    if(button.dataset.ifTab==='new')openForgeCatalogPicker(row=>{reference=null;catalogItem=row;current=null;baseItem=row;location='New item · not saved yet';created=false;creationUncertain=false;saved=false;editor.setBase({},'');fill(null);showBase();message('');setTab('properties');setStep(2)});
    else if(button.dataset.ifTab==='signature')openForgeSignaturePicker((sig,row)=>{reference=null;catalogItem=row;current=null;baseItem={...row,signature:sig.name};location=`Signature item · ${sig.base.name} base · not saved yet`;created=false;creationUncertain=false;saved=false;editor.setBase({},'');fill(sig.config);showBase();message(`${sig.name} is ready: press FORGE ITEM to create it in the Shared Stash. ${sig.needs}.`);setTab('properties');setStep(2)});
    else openForgeOwnedPicker(selectReference);
  });
  app.querySelectorAll('[data-forge-step]').forEach(button=>button.onclick=()=>{if(!busy)setStep(+button.dataset.forgeStep)});
  app.querySelectorAll('[data-edit-tab]').forEach(button=>{button.onclick=()=>setTab(button.dataset.editTab);button.onkeydown=event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const tabs=[...app.querySelectorAll('[data-edit-tab]')],index=tabs.indexOf(button),next=tabs[(index+(event.key==='ArrowRight'?1:2))%3];setTab(next.dataset.editTab);next.focus()}});
  ['ifname','ifrarity','iflore','ifaffix','iftab'].forEach(id=>{const input=q(id);input.oninput=()=>{message('');update()};if(input.tagName==='SELECT')input.onchange=input.oninput});
  app.querySelectorAll('[name=forge-mechanic]').forEach(radio=>radio.onchange=()=>{q('ifmech').value=radio.value;message('');update()});
  q('ifnext').onclick=()=>{const state=editor.state();if(state.err){setTab('properties');message(state.err);editor.focusInvalid(state.key);return}setStep(3)};
  q('ifeditback').onclick=()=>setStep(2);q('ifback').onclick=()=>setStep(Math.max(1,step-1));
  q('ifreset').onclick=()=>{if(confirm('Discard your unsaved changes to this item?')){fill(current?.configuration);message('');update()}};
  q('ifgo').onclick=async()=>{
    if(busy||GAME_RUNNING||creationUncertain||!baseItem)return;const state=editor.state();if(state.err){message(state.err);return}if(!Object.keys(state.stats).length){message('Add at least one property before saving.');return}
    const {name,lore,affix,rarity}=identity();if(affix.split(/\r?\n/).filter(line=>line.trim()).length>3){setStep(2);setTab('appearance');message('Special text can contain up to 3 non-empty lines.');q('ifaffix').focus();return}
    busy=true;message('');update();q('ifworkspace').inert=true;
    try{
      if(!reference&&catalogItem){
        let result;
        try{result=await j('/api/item-forge/create',{method:'POST',body:JSON.stringify({cid:+catalogItem.id,tab:q('iftab').value})})}
        catch(error){creationUncertain=true;throw Error('The connection was interrupted during creation. Check Shared Stash using Choose item → Customize an owned item. A second item will not be created automatically.')}
        if(result.err)throw Error(result.err);
        // Keep the exact newly created target if applying properties fails; Retry never duplicates it.
        reference={target:result.target,key:result.key};created=true;location=result.target.tab==='unique_items'?'Shared Stash · Unique items':q('iftab').selectedOptions[0].textContent;showBase();
      }
      const body={action:'apply',...reference,stats:state.stats,presetIds:state.presetIds,excludedKeys:state.excludedKeys,keepNative:state.keepNative,name:name||null,lore:lore||null,affix:affix||null,rarity:rarity===''?null:+rarity,mechanic:q('ifmech').value||null};
      const result=await j('/api/custom-forge',{method:'POST',body:JSON.stringify(body)});if(result.err)throw Error(result.err);
      current={...current,configuration:result.configuration};catalogItem=null;created=false;saved=true;fill(result.configuration);showBase();
      if(result.runtime)q('ifruntime').innerHTML=forgeRuntimeBanner(result.runtime);
      message(`Saved ${result.configuration?.name||baseItem.name}. Start Hero Siege with ForgePact to apply it.`,'success');
    }catch(error){message((created?'The base item is in '+location+', but its forge is not saved yet. Retry Save to finish it. ':'')+(error.message||'Saving was interrupted.'))}
    finally{busy=false;q('ifworkspace').inert=false;update()}
  };
  q('ifremove').onclick=async()=>{
    if(busy||GAME_RUNNING||!reference||!confirm('Remove this item’s saved Forge setup? The item itself will remain.'))return;
    busy=true;update();q('ifworkspace').inert=true;
    try{const result=await j('/api/custom-forge',{method:'POST',body:JSON.stringify({action:'remove',...reference})});if(result.err)throw Error(result.err);current={...current,configuration:null};saved=true;fill(null);showBase();message('Forge removed. The item remains in its current location. Restart the game to see its original setup.','success')}
    catch(error){message(error.message||'Could not remove the forge.')}finally{busy=false;q('ifworkspace').inert=false;update()}
  };
  setTab('properties');
  j('/api/custom-forge/runtime').then(runtime=>{if(live()){q('ifruntime').innerHTML=forgeRuntimeBanner(runtime);q('ifstart-runtime').innerHTML=forgeRuntimeBanner(runtime)}}).catch(()=>{if(live())q('ifstart-runtime').innerHTML=forgeRuntimeBanner({level:'warn',message:'ForgePact status could not be checked. You can still prepare your item.'})});
  if(preset?.ref)await selectReference(preset.ref,preset.label,preset.item);else update();
}

// Keep drafts intact when navigating out of the workshop, and refresh save locks.
document.addEventListener('click',event=>{
  const navigation=event.target.closest('#left .charbtn,#left .tabbtn');if(!navigation||navigation.dataset.view==='itemforge')return;
  if(FORGE_SESSION?.app.isConnected&&FORGE_SESSION.dirty()){
    if(FORGE_SESSION.isBusy()||!confirm('Leave Item Forge and discard your unsaved draft?')){event.preventDefault();event.stopImmediatePropagation();return}
  }
  FORGE_SESSION=null;
},true);
window.addEventListener('beforeunload',event=>{if(FORGE_SESSION?.dirty()){event.preventDefault();event.returnValue=''}});

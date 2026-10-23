import fs from 'node:fs/promises';
import path from 'node:path';
import {PresentationFile,FileBlob} from 'file:///C:/Users/ricat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs';
import {finalizePresentation} from 'file:///C:/Users/ricat/.codex/plugins/cache/openai-primary-runtime/presentations/26.1022.11436/skills/presentations/container_tools/artifact_tool_utils.mjs';
const p=await PresentationFile.importPptx(await FileBlob.load('HR-Agent/presentation/hr-assistant-presentation-qa-improvements.pptx'));
const original=[...p.slides.items];
const slideIds=(await p.inspect({kind:'slide',maxChars:50000})).records.filter(x=>x.kind==='slide').map(x=>x.id);
const titleOf=s=>s.shapes.items.map(x=>String(x.text)).find(t=>t==='Limitations and next steps'||t.startsWith('Q&A:'));
const test=original.find(s=>titleOf(s)==='Q&A: test evidence and deployment');
const edits={'2':'Q&A: test evidence','4':'Latest test suite','5':'290 non-live tests passed\n31 database tests skipped','6':'Live-provider checks','7':'4 separate checks passed\nIndonesian and English prompts','8':'Verification scope','9':'Local policies and synthetic memory storage\nCitations checked, no action writes','10':'Latest recorded results. Suite and live-provider checks have separate scopes.'};
for(const [id,text] of Object.entries(edits)){const shape=test.shapes.items.find(s=>s.id===id);shape.text.replace(String(shape.text),text);}
const closingIndex=original.findIndex(s=>titleOf(s)==='Limitations and next steps');
const order=original.map((s,i)=>i).filter(i=>i!==closingIndex&&titleOf(original[i])!=='Q&A: residual risks');
order.push(closingIndex);
p.slides.keep(order);
p.slides.reorder(order.map(i=>slideIds[i]));
const notes=(await fs.readFile('HR-Agent/presentation/speaker-notes.md','utf8')).split(/^## Slide \d+\s*\n/m).slice(1);
for(const [i,s] of p.slides.items.entries()){
 const oldIndex=original.indexOf(s);
 for(const shape of s.shapes.items){
  const text=String(shape.text);
  if(/^\d{2}$/.test(text)&&shape.position.top>600)shape.text.replace(text,String(i+1).padStart(2,'0'));
  if(text.includes('Cloud verification pending'))shape.text.replace('\nCloud verification pending','');
  if(text.includes('Deployed PostgreSQL checks pending.'))shape.text.replace(' Deployed PostgreSQL checks pending.','');
  if(text.includes('; database verification pending.'))shape.text.replace('; database verification pending.','.');
 }
 s.speakerNotes.text=notes[i].trim();
 await fs.writeFile(`.tmp-presentation/reordered-${i+1}.png`,new Uint8Array(await (await s.export({format:'png',scale:1})).arrayBuffer()));
}
const candidate=path.resolve('.tmp-presentation/reordered-candidate.pptx');
const final=path.resolve('HR-Agent/presentation/hr-assistant-presentation-qa-final-checked.pptx');
await (await PresentationFile.exportPptx(p)).save(candidate);
const skill='C:/Users/ricat/.codex/plugins/cache/openai-primary-runtime/presentations/26.1022.11436/skills/presentations';
try{
 await finalizePresentation({workspaceDir:process.cwd(),candidatePath:candidate,finalPath:final,pythonExecutable:'C:/Users/ricat/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe',integrityValidatorPath:skill+'/container_tools/inspect_presentation_package_integrity.py',layoutValidatorPath:skill+'/container_tools/inspect_presentation_layout_geometry.py',explicitTotalSlideCount:22,layoutArgs:['--expected-slide-size-emu','12192000,6858000'],verifyArtifactToolImport:true,receiptPath:path.resolve('.tmp-presentation/reordered-validation.json')});
}catch(e){
 if(e.code==='EPERM'&&e.syscall==='link'&&e.dest===final){await fs.copyFile(candidate,final,1);console.log('Validated file published with copy because Windows blocked hard-link publication.');}
 else throw e;
}
console.log(p.slides.items.map((s,i)=>`${i+1}: ${String(s.shapes.items[0].text)}`).join('\n'));

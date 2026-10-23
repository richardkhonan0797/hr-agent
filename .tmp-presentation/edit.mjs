import fs from 'node:fs/promises';
import path from 'node:path';
import { PresentationFile, FileBlob } from 'file:///C:/Users/ricat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs';
import { finalizePresentation } from 'file:///C:/Users/ricat/.codex/plugins/cache/openai-primary-runtime/presentations/26.1022.11436/skills/presentations/container_tools/artifact_tool_utils.mjs';
const p = await PresentationFile.importPptx(await FileBlob.load('HR-Agent/presentation/hr-assistant-presentation-qa-reasons.pptx'));
const s=p.slides.items[6];
s.shapes.items.find(x=>x.id==='5').text.replace('Demo authentication','Regex intent classifier for demo only\nCan miss wording and negation\nDemo authentication');
s.shapes.items.find(x=>x.id==='7').text.replace('Production identity management','LLM intent classifier with structured output\nBackend rules remain enforced\nProduction identity management');
const notes=await fs.readFile('HR-Agent/presentation/speaker-notes.md','utf8');
s.speakerNotes.text=notes.replaceAll('\r', '').split('## Slide 7\n')[1].split('## Slide 8')[0].trim();
await fs.writeFile('.tmp-presentation/after.png',new Uint8Array(await (await s.export({format:'png',scale:1})).arrayBuffer()));
await (await PresentationFile.exportPptx(p)).save('.tmp-presentation/candidate.pptx');
const skill='C:/Users/ricat/.codex/plugins/cache/openai-primary-runtime/presentations/26.1022.11436/skills/presentations';
const result=await finalizePresentation({workspaceDir:process.cwd(),candidatePath:path.resolve('.tmp-presentation/candidate.pptx'),finalPath:path.resolve('HR-Agent/presentation/hr-assistant-presentation-qa-improvements.pptx'),pythonExecutable:'C:/Users/ricat/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe',integrityValidatorPath:skill+'/container_tools/inspect_presentation_package_integrity.py',layoutValidatorPath:skill+'/container_tools/inspect_presentation_layout_geometry.py',explicitTotalSlideCount:p.slides.items.length,layoutArgs:['--expected-slide-size-emu','12192000,6858000'],verifyArtifactToolImport:true,receiptPath:path.resolve('.tmp-presentation/validation.json')});
console.log(JSON.stringify(result));


from pathlib import Path
import re,zipfile,xml.etree.ElementTree as E,posixpath
root=Path('HR-Agent/presentation')
z=zipfile.ZipFile(root/'hr-assistant-presentation-qa-final.pptx')
ns={'a':'http://schemas.openxmlformats.org/drawingml/2006/main','p':'http://schemas.openxmlformats.org/presentationml/2006/main'}
rels={x.attrib['Id']:x.attrib['Target'] for x in E.fromstring(z.read('ppt/_rels/presentation.xml.rels'))}
slides=[]
for item in E.fromstring(z.read('ppt/presentation.xml')).find('p:sldIdLst',ns):
    target=rels[item.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
    entry=target.lstrip('/') if target.startswith('/') else posixpath.normpath('ppt/'+target)
    slide=E.fromstring(z.read(entry))
    title=next(''.join(t.itertext()) for t in slide.findall('.//a:t',ns) if (t.text or '').strip())
    slides.append(title)
assert len(slides)==21 and slides[-1]=='Limitations and next steps'
p=root/'speaker-notes.md'
t=p.read_text(encoding='utf-8-sig')
parts=re.split(r'^## Slide (\d+)\s*\n',t,flags=re.M)
old={int(parts[i]):parts[i+1].strip() for i in range(1,len(parts),2)}
order=list(range(1,16))+list(range(17,23))
def refs(t):
    return t.replace('slide 22','slide 21').replace('Slide 22','Slide 21').replace('slides 17–21','slides 16–20').replace('Slides 17–21','Slides 16–20')
intro='''# English speaker notes

These notes follow the current saved `hr-assistant-presentation-qa-final.pptx`, slide by slide. The deck has 21 slides. Slides 1–6 cover the main presentation, slides 7–14 contain recorded evidence, slide 15 covers API boundaries, slides 16–20 contain the HR QA prompts, and slide 21 closes with limitations and next steps.

For the 12-minute core talk, use slides 1–6 and then slide 21. The sections below remain in the physical deck order, including optional evidence and QA slides.
'''
contents=[refs(old[n]) for n in order]
p.write_text(intro+'\n'+ '\n\n'.join(f'## Slide {i} — {title}\n\n{body}' for i,(title,body) in enumerate(zip(slides,contents),1))+'\n',encoding='utf-8')
p=root/'rehearsal-guide.md'
t=refs(p.read_text(encoding='utf-8'))
t=t.replace('with 22 slides','with 21 slides').replace('| 22 |','| 21 |')
t=t.replace('- Slide 16: latest recorded test evidence, without dates or deployment status.\n','')
t=re.sub(r'Appendix slide: (\d+)',lambda m:f'Appendix slide: {int(m[1])-1}',t)
t=t.replace('Slide 16 shows the latest recorded non-live suite:', 'Reference for questions about tests: the latest recorded non-live suite reports')
a=t.index('## Timing cues')
t=t[:a]+'''## Complete slide order

| Slide | Title |
|---|---|
'''+''.join(f'| {i} | {title} |\n' for i,title in enumerate(slides,1))+'\n'+t[a:]
p.write_text(t,encoding='utf-8')
assert len(re.findall(r'^## Slide \d+ — ',(root/'speaker-notes.md').read_text(encoding='utf-8'),re.M))==len(slides)
print('\n'.join(f'{i}: {title}' for i,title in enumerate(slides,1)))

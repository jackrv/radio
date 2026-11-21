const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../local_admin/js/12-recordings.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('  function wavSpeechGuide('), source.indexOf('  function wavPeaks(')) +
  '\nglobalThis.guide = wavSpeechGuide; globalThis.cues = approximateWordCues;', context);

function wav(seconds, intervals, bits=16) {
  const rate=16000, frames=Math.round(rate*seconds), buffer=new ArrayBuffer(44+frames*2), v=new DataView(buffer);
  const str=(at,s)=>{for(let i=0;i<s.length;i++)v.setUint8(at+i,s.charCodeAt(i));};
  str(0,'RIFF');v.setUint32(4,36+frames*2,true);str(8,'WAVE');str(12,'fmt ');v.setUint32(16,16,true);
  v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,rate,true);v.setUint32(28,rate*2,true);
  v.setUint16(32,2,true);v.setUint16(34,bits,true);str(36,'data');v.setUint32(40,frames*2,true);
  for(let i=0;i<frames;i++)if(intervals.some(([a,b])=>a<=i/rate&&i/rate<b))v.setInt16(44+i*2,Math.round(8000*Math.sin(i*.1)),true);
  return buffer;
}

test('approximate guide skips long silence and preserves audio time coordinates',()=>{
  const guide=context.guide(wav(3,[[.3,1],[1.8,2.7]]));
  assert.equal(guide.duration,3);assert.equal(guide.spans.length,2);
  assert.ok(guide.spans[0][0]>=.24&&guide.spans[0][1]<=1.06);
  assert.ok(guide.spans[1][0]>=1.74&&guide.spans[1][1]<=2.76);
  const cues=context.cues('Клен, прийнято / Дуб, добре',guide);
  assert.equal(cues.length,4);
  assert.ok(cues.every((c,i)=>c.start>=0&&c.end>=c.start&&c.end<=3&&(!i||c.start>=cues[i-1].end)));
});

test('word length, numbers, punctuation and emoji affect only approximate positions',()=>{
  const text='📻 Я, довжелезне / 73', guide={duration:10,spans:[[0,10]]};
  const cues=context.cues(text,guide);
  assert.deepEqual(Array.from(cues,c=>text.slice(c.from,c.to)),['Я','довжелезне','73']);
  assert.ok(cues[1].end-cues[1].start>cues[0].end-cues[0].start);
  assert.equal(context.cues(' / !!!',guide).length,0);
  assert.equal(context.cues('',guide).length,0);
  assert.equal(context.cues(text,null).length,0);
  assert.equal(context.cues('Нове слово',guide).length,2);
});

test('silent and unsupported WAV never invent spoken activity',()=>{
  assert.equal(context.guide(wav(2,[])).spans.length,0);
  assert.equal(context.cues('текст',context.guide(wav(2,[]))).length,0);
  assert.equal(context.guide(wav(2,[],32)),null);
  assert.equal(context.guide(new ArrayBuffer(4)),null);
});

// Pure PCM encoder test; does not exercise a real browser microphone.
import fs from 'node:fs';
import assert from 'node:assert/strict';
const source=fs.readFileSync(new URL('../studio/static/recorder.js',import.meta.url),'utf8');
const {encodeWav}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const blob=encodeWav([new Float32Array([-1,0,1])],48000);
const bytes=Buffer.from(await blob.arrayBuffer());
assert.equal(bytes.toString('ascii',0,4),'RIFF');
assert.equal(bytes.toString('ascii',8,12),'WAVE');
assert.equal(bytes.length,50);
assert.equal(bytes.readUInt32LE(24),48000);
assert.equal(bytes.readUInt16LE(22),1);
assert.equal(bytes.readUInt16LE(34),16);
assert.equal(bytes.readUInt32LE(40),6);
assert.equal(bytes.readInt16LE(44),-32768);
assert.equal(bytes.readInt16LE(46),0);
assert.equal(bytes.readInt16LE(48),32767);
console.log('PCM WAV encoder assertions passed. No real microphone was used.');

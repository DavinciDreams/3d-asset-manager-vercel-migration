#!/usr/bin/env node
import fs from 'node:fs/promises';
import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { unpartition } from '@gltf-transform/functions';
import { MeshoptDecoder } from 'meshoptimizer';

const GLB_JSON_CHUNK = 0x4e4f534a;

function usage() {
  console.error('Usage: node decode-meshopt-glb.mjs <input.glb> <output.glb>');
  process.exit(2);
}

function paddedJsonBytes(value) {
  const encoded = Buffer.from(JSON.stringify(value), 'utf8');
  const padding = (4 - (encoded.length % 4)) % 4;
  return padding ? Buffer.concat([encoded, Buffer.alloc(padding, 0x20)]) : encoded;
}

function prepareMeshoptGLB(input) {
  if (input.length < 20 || input.subarray(0, 4).toString('ascii') !== 'glTF') {
    throw new Error('Input must be a binary GLB.');
  }
  const version = input.readUInt32LE(4);
  const declaredLength = input.readUInt32LE(8);
  if (version !== 2 || declaredLength > input.length) {
    throw new Error('Invalid GLB header.');
  }

  const chunks = [];
  let json = null;
  let offset = 12;
  while (offset + 8 <= declaredLength) {
    const length = input.readUInt32LE(offset);
    const type = input.readUInt32LE(offset + 4);
    const start = offset + 8;
    const end = start + length;
    if (end > declaredLength) {
      throw new Error('Invalid GLB chunk length.');
    }
    let data = input.subarray(start, end);
    if (type === GLB_JSON_CHUNK) {
      json = JSON.parse(data.toString('utf8').replace(/[\s\0]+$/g, ''));
    }
    chunks.push({ type, data });
    offset = end;
  }
  if (!json) {
    throw new Error('GLB JSON chunk not found.');
  }

  for (const bufferView of json.bufferViews || []) {
    const meshopt = bufferView.extensions?.EXT_meshopt_compression;
    if (!meshopt) continue;
    bufferView.buffer = meshopt.buffer ?? 0;
    bufferView.byteOffset = meshopt.byteOffset ?? 0;
    bufferView.byteLength = meshopt.byteLength ?? 0;
    delete bufferView.byteStride;
  }

  json.buffers = (json.buffers || []).filter((buffer) => !buffer.uri);

  const outputChunks = chunks.map((chunk) => ({
    type: chunk.type,
    data: chunk.type === GLB_JSON_CHUNK ? paddedJsonBytes(json) : chunk.data,
  }));
  const outputLength = 12 + outputChunks.reduce((total, chunk) => total + 8 + chunk.data.length, 0);
  const output = Buffer.alloc(outputLength);
  output.write('glTF', 0, 'ascii');
  output.writeUInt32LE(2, 4);
  output.writeUInt32LE(outputLength, 8);
  offset = 12;
  for (const chunk of outputChunks) {
    output.writeUInt32LE(chunk.data.length, offset);
    output.writeUInt32LE(chunk.type, offset + 4);
    chunk.data.copy(output, offset + 8);
    offset += 8 + chunk.data.length;
  }
  return output;
}

const [, , inputPath, outputPath] = process.argv;
if (!inputPath || !outputPath) usage();

await MeshoptDecoder.ready;
const input = await fs.readFile(inputPath);
const prepared = prepareMeshoptGLB(input);
const io = new NodeIO()
  .registerExtensions(ALL_EXTENSIONS)
  .registerDependencies({ 'meshopt.decoder': MeshoptDecoder });
const document = await io.readBinary(prepared);

for (const extension of document.getRoot().listExtensionsUsed()) {
  if (extension.extensionName === 'EXT_meshopt_compression') {
    extension.dispose();
  }
}

await document.transform(unpartition());
await io.write(outputPath, document);

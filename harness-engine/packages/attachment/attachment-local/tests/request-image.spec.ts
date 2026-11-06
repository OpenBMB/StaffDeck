import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import sharp from 'sharp'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CompressionLimiter } from '../src/compression-limiter.ts'
import LocalAttachmentStore from '../src/index.ts'

const homes: string[] = []

async function store(): Promise<LocalAttachmentStore> {
  const dshHome = await mkdtemp(join(tmpdir(), 'dsh-request-image-'))
  homes.push(dshHome)
  return new LocalAttachmentStore(new Context(), { dshHome })
}

async function image(width: number, height: number): Promise<Uint8Array> {
  return new Uint8Array(await sharp({
    create: { width, height, channels: 3, background: { r: 12, g: 34, b: 56 } },
  }).png().toBuffer())
}

async function complexOpaqueAlphaImage(width: number, height: number): Promise<Uint8Array> {
  const pixels = new Uint8Array(width * height * 4)
  let state = 0x2545f491
  for (let offset = 0; offset < pixels.length; offset += 4) {
    for (let channel = 0; channel < 3; channel += 1) {
      state ^= state << 13
      state ^= state >>> 17
      state ^= state << 5
      pixels[offset + channel] = state & 0xff
    }
    pixels[offset + 3] = 255
  }
  return new Uint8Array(await sharp(pixels, {
    raw: { width, height, channels: 4 },
  }).png().toBuffer())
}

afterEach(async () => {
  await Promise.all(homes.splice(0).map(home => rm(home, { recursive: true, force: true })))
})

describe('local request-image cache', () => {
  it('passes through an in-budget attachment and composes ordered request reads', async () => {
    const attachments = await store()
    const first = await attachments.saveImage({ data: await image(8, 4), mediaType: 'image/png' })
    const second = await attachments.saveImage({ data: await image(4, 8), mediaType: 'image/png' })
    const firstStored = await attachments.readImage(first)
    const policy = { maxPixels: 1_000, maxBytes: 1024 * 1024 }

    const request = await attachments.readImageRequest(first, policy)
    const batch = await Promise.all([first, second].map(
      attachment => attachments.readImageRequest(attachment, policy),
    ))

    expect(request.data).toEqual(firstStored.data)
    expect(batch.map(value => value.attachment.attachmentId)).toEqual([first.attachmentId, second.attachmentId])
  })

  it('rejects invalid request policies', async () => {
    const attachments = await store()
    const attachment = await attachments.saveImage({ data: await image(8, 4), mediaType: 'image/png' })

    await expect(attachments.readImageRequest(attachment, { maxPixels: 0, maxBytes: 100 }))
      .rejects.toThrow('Image request maxPixels must be a positive integer')
    await expect(attachments.readImageRequest(attachment, { maxPixels: 100, maxBytes: 0 }))
      .rejects.toThrow('Image request maxBytes must be a positive integer')
  })

  it('keeps the smallest ladder output when the encoded-byte target is unreachable', async () => {
    const attachments = await store()
    const attachment = await attachments.saveImage({ data: await image(1, 1), mediaType: 'image/png' })

    const request = await attachments.readImageRequest(attachment, { maxPixels: 1, maxBytes: 1 })

    expect(request.mediaType).toBe('image/jpeg')
    expect(request.bytes).toBeGreaterThan(1)
    expect(request).toMatchObject({ width: 1, height: 1 })
  })

  it('regenerates invalid, oversized, incompatible, or mismatched cached variants', async () => {
    const attachments = await store()
    const attachment = await attachments.saveImage({ data: await image(64, 32), mediaType: 'image/png' })
    const policy = { maxPixels: 16 * 16, maxBytes: 4_096 }
    const initial = await attachments.readImageRequest(attachment, policy)
    const hash = String(initial.variantId).slice('sha256:'.length)
    const path = join(attachments.root, 'request-images', hash.slice(0, 2), hash)
    const noisyPixels = new Uint8Array(64 * 64 * 3)
    let state = 0x2545f491
    for (let index = 0; index < noisyPixels.length; index += 1) {
      state ^= state << 13
      state ^= state >>> 17
      state ^= state << 5
      noisyPixels[index] = state & 0xff
    }
    const oversized = new Uint8Array(await sharp(noisyPixels, {
      raw: { width: 64, height: 64, channels: 3 },
    }).png().toBuffer())
    const depth16 = new Uint8Array(await sharp({
      create: { width: 16, height: 8, channels: 3, background: { r: 1, g: 2, b: 3 } },
    }).toColourspace('rgb16').png().toBuffer())
    const cmyk = new Uint8Array(await sharp({
      create: { width: 16, height: 8, channels: 3, background: { r: 1, g: 2, b: 3 } },
    }).toColourspace('cmyk').jpeg().toBuffer())
    const tooWide = await image(23, 11)
    const unexpectedAlpha = new Uint8Array(await sharp({
      create: { width: 16, height: 8, channels: 4, background: { r: 1, g: 2, b: 3, alpha: 0.5 } },
    }).png().toBuffer())

    for (const invalid of [
      oversized,
      depth16,
      cmyk,
      tooWide,
      unexpectedAlpha,
      Uint8Array.of(1, 2, 3),
    ]) {
      await writeFile(path, invalid)
      const regenerated = await attachments.readImageRequest(attachment, policy)
      expect(regenerated.data).toEqual(initial.data)
    }
  })

  it('derives stable square and wide previews and separates route budgets in the cache key', async () => {
    const attachments = await store()
    const square = await attachments.saveImage({
      data: await image(2048, 2048), mediaType: 'image/png', name: 'square.png',
    })
    const wide = await attachments.saveImage({
      data: await image(2048, 1024), mediaType: 'image/png', name: 'wide.png',
    })

    const squareRequest = await attachments.readImageRequest(square, { maxPixels: 640_000, maxBytes: 1024 * 1024 })
    const wideRequest = await attachments.readImageRequest(wide, { maxPixels: 640_000, maxBytes: 1024 * 1024 })
    const repeated = await attachments.readImageRequest(wide, { maxPixels: 640_000, maxBytes: 1024 * 1024 })
    const low = await attachments.readImageRequest(wide, { maxPixels: 512 * 512, maxBytes: 1024 * 1024 })

    expect(squareRequest).toMatchObject({ width: 800, height: 800 })
    expect(wideRequest).toMatchObject({ width: 1130, height: 565 })
    expect(repeated.variantId).toBe(wideRequest.variantId)
    expect(repeated.data).toEqual(wideRequest.data)
    expect(Buffer.from(repeated.data).toString('base64')).toBe(Buffer.from(wideRequest.data).toString('base64'))
    expect(low.variantId).not.toBe(wideRequest.variantId)
    expect(low.width * low.height).toBeLessThanOrEqual(512 * 512 + low.width)
  })

  it('routes opaque pixels to JPEG and preserves alpha on the WebP ladder', async () => {
    const attachments = await store()
    const side = 256
    const photoPixels = new Uint8Array(side * side * 3)
    const alphaPixels = new Uint8Array(side * side * 4)
    let state = 0x2545f491
    for (let pixel = 0; pixel < side * side; pixel += 1) {
      state ^= state << 13
      state ^= state >>> 17
      state ^= state << 5
      const photo = pixel * 3
      const alpha = pixel * 4
      photoPixels[photo] = state & 0xff
      photoPixels[photo + 1] = state >> 8 & 0xff
      photoPixels[photo + 2] = state >> 16 & 0xff
      alphaPixels[alpha] = photoPixels[photo] ?? 0
      alphaPixels[alpha + 1] = photoPixels[photo + 1] ?? 0
      alphaPixels[alpha + 2] = photoPixels[photo + 2] ?? 0
      alphaPixels[alpha + 3] = pixel & 0xff
    }
    const photoSource = new Uint8Array(await sharp(photoPixels, {
      raw: { width: side, height: side, channels: 3 },
    }).png().toBuffer())
    const alphaSource = new Uint8Array(await sharp(alphaPixels, {
      raw: { width: side, height: side, channels: 4 },
    }).png().toBuffer())
    const photo = await attachments.saveImage({ data: photoSource, mediaType: 'image/png' })
    const alpha = await attachments.saveImage({ data: alphaSource, mediaType: 'image/png' })

    const photoRequest = await attachments.readImageRequest(photo, { maxPixels: 128 * 128, maxBytes: 1024 * 1024 })
    const alphaRequest = await attachments.readImageRequest(alpha, { maxPixels: 128 * 128, maxBytes: 4_096 })

    expect(photoRequest.mediaType).toBe('image/jpeg')
    expect(alphaRequest.mediaType).toBe('image/webp')
    expect(alphaRequest.bytes).toBeGreaterThan(4_096)
    expect(alphaRequest).toMatchObject({ width: 128, height: 128 })
    await expect(sharp(alphaRequest.data).metadata()).resolves.toMatchObject({ hasAlpha: true, depth: 'uchar', space: 'srgb' })
  })

  it.each([3, 4] as const)('projects a 16-bit %s-channel PNG as a bounded 8-bit request image', async (channels) => {
    const attachments = await store()
    const source = new Uint8Array(await sharp({
      create: { width: 64, height: 32, channels, background: { r: 12, g: 34, b: 56, alpha: 0.5 } },
    }).toColourspace('rgb16').png().toBuffer())
    const attachment = await attachments.saveImage({ data: source, mediaType: 'image/png' })

    const request = await attachments.readImageRequest(attachment, { maxPixels: 16 * 16, maxBytes: 1024 * 1024 })

    expect(request.bytes).toBeLessThanOrEqual(1024 * 1024)
    expect(request.width * request.height).toBeLessThanOrEqual(16 * 16)
    await expect(sharp(request.data).metadata()).resolves.toMatchObject({
      depth: 'uchar', space: 'srgb', hasAlpha: channels === 4,
    })
  })

  it('accepts a resized WebP request version that omits an all-opaque alpha plane', async () => {
    const attachments = await store()
    const source = await complexOpaqueAlphaImage(64, 32)
    const attachment = await attachments.saveImage({ data: source, mediaType: 'image/png' })

    const request = await attachments.readImageRequest(attachment, { maxPixels: 16 * 16, maxBytes: 1024 * 1024 })

    expect(request.mediaType).toBe('image/webp')
    await expect(sharp(request.data).metadata()).resolves.toMatchObject({ hasAlpha: false })
  })

  it('keeps a complex 640,000-pixel request version below 1 MiB', async () => {
    const attachments = await store()
    const side = 1024
    const pixels = new Uint8Array(side * side * 3)
    let state = 0x6d2b79f5
    for (let index = 0; index < pixels.length; index += 1) {
      state ^= state << 13
      state ^= state >>> 17
      state ^= state << 5
      pixels[index] = state & 0xff
    }
    const source = new Uint8Array(await sharp(pixels, {
      raw: { width: side, height: side, channels: 3 },
    }).png().toBuffer())
    const attachment = await attachments.saveImage({ data: source, mediaType: 'image/png' })

    const request = await attachments.readImageRequest(attachment, { maxPixels: 640_000, maxBytes: 1024 * 1024 })

    expect(request).toMatchObject({ width: 800, height: 800 })
    expect(request.bytes).toBeLessThanOrEqual(1024 * 1024)
  })

  it('shares one request transform between concurrent callers without sharing cancellation', async () => {
    const attachments = await store()
    const attachment = await attachments.saveImage({
      data: await image(2048, 1024), mediaType: 'image/png', name: 'shared.png',
    })
    const run = vi.spyOn(CompressionLimiter.prototype, 'run')
    const controller = new AbortController()
    const policy = { maxPixels: 640_000, maxBytes: 1024 * 1024 }

    const cancelled = attachments.readImageRequest(attachment, policy, controller.signal)
    const completed = attachments.readImageRequest(attachment, policy)
    const reason = new Error('cancel one waiter')
    controller.abort(reason)

    await expect(cancelled).rejects.toBe(reason)
    await expect(completed).resolves.toMatchObject({ width: 1130, height: 565 })
    expect(run).toHaveBeenCalledTimes(1)
    run.mockRestore()
  })

  it('aborts the underlying request transform after its only waiter cancels', async () => {
    const attachments = await store()
    const attachment = await attachments.saveImage({
      data: await image(2048, 1024), mediaType: 'image/png', name: 'cancelled.png',
    })
    let readSignal: AbortSignal | undefined
    const read = vi.spyOn(attachments, 'readImage').mockImplementation((_ref, signal) => {
      readSignal = signal
      return new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => {
          reject(new Error('request transform aborted', { cause: signal.reason }))
        }, { once: true })
      })
    })
    const controller = new AbortController()
    const request = attachments.readImageRequest(
      attachment,
      { maxPixels: 640_000, maxBytes: 1024 * 1024 },
      controller.signal,
    )
    await vi.waitFor(() => {
      expect(read).toHaveBeenCalledTimes(1)
    })

    const reason = new Error('cancel only transform waiter')
    controller.abort(reason)

    await expect(request).rejects.toBe(reason)
    expect(readSignal?.reason).toBe(reason)
  })

  it('normalizes a non-Error cancellation and replaces an aborted shared transform', async () => {
    const attachments = await store()
    const attachment = await attachments.saveImage({
      data: await image(2048, 1024), mediaType: 'image/png', name: 'replace.png',
    })
    const actualRead = attachments.readImage.bind(attachments)
    let calls = 0
    vi.spyOn(attachments, 'readImage').mockImplementation((ref, signal) => {
      calls += 1
      if (calls === 1) {
        return new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => {
            reject(new Error('request transform aborted', { cause: signal.reason }))
          }, { once: true })
        })
      }
      return actualRead(ref, signal)
    })
    const controller = new AbortController()
    const policy = { maxPixels: 640_000, maxBytes: 1024 * 1024 }
    const cancelled = attachments.readImageRequest(attachment, policy, controller.signal)
    await vi.waitFor(() => {
      expect(calls).toBe(1)
    })

    controller.abort('cancelled')
    const replacement = attachments.readImageRequest(attachment, policy)

    await expect(cancelled).rejects.toMatchObject({
      message: 'Attachment request cancelled with a non-Error reason.',
      cause: 'cancelled',
    })
    await expect(replacement).resolves.toMatchObject({ width: 1130, height: 565 })
    expect(calls).toBe(2)
  })

})

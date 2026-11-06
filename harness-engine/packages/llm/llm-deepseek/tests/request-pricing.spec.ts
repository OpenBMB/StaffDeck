import { describe, expect, it } from 'vitest'
import { offloadedImageText, requestImageHandleText, textOnlyImageText } from '@deepseek-ai/dsh-llm'
import { AttachmentId } from '@deepseek-ai/dsh-attachment'
import type { ImageAttachmentRef } from '@deepseek-ai/dsh-attachment'
import { deepSeekImageRequestPricing } from '../src/request-pricing.ts'
import { resolveAdapterOptions } from '../src/index.ts'
import type { Config } from '../src/index.ts'

const VISION_MODEL = {
  id: 'vision',
  inputModalities: ['text', 'image'] as Array<'text' | 'image'>,
}

function ref(name: string, width: number, height: number, bytes = 1024): ImageAttachmentRef {
  return {
    attachmentId: AttachmentId(`sha256:${name.padEnd(8, '0')}`),
    mediaType: 'image/png',
    bytes,
    width,
    height,
    name,
  }
}

function connection(config: Omit<Config, 'models'> = {}): ReturnType<typeof resolveAdapterOptions> {
  return resolveAdapterOptions(Object.assign({ models: [VISION_MODEL] }, config))
}

describe('DeepSeek request-image pricing', () => {
  it('prices an uncatalogued model as its text-only substitution', () => {
    const image = ref('photo', 1920, 1080)
    const prices = deepSeekImageRequestPricing(connection(), 'unlisted').priceImages([image])
    expect(prices).toEqual([{ visualTokens: 0, text: textOnlyImageText(image) }])
  })

  it('prices a catalogued text-only model as its text-only substitution', () => {
    const image = ref('photo', 1920, 1080)
    const options = resolveAdapterOptions({ models: [{ id: 'text-only' }] })
    const prices = deepSeekImageRequestPricing(options, 'text-only').priceImages([image])
    expect(prices).toEqual([{ visualTokens: 0, text: textOnlyImageText(image) }])
  })

  it('prices a retained image by its projected request dimensions plus its handle text', () => {
    const image = ref('photo', 1920, 1080)
    const prices = deepSeekImageRequestPricing(connection(), 'vision').priceImages([image])
    expect(prices).toEqual([{
      visualTokens: 369,
      text: requestImageHandleText(image, { width: 1066, height: 600 }),
    }])
  })

  it('honors the low-detail pixel budget preset', () => {
    const image = ref('photo', 4096, 4096)
    const options = resolveAdapterOptions({
      models: [{ ...VISION_MODEL, imagePixelBudget: 'low' as const }],
    })
    const prices = deepSeekImageRequestPricing(options, 'vision').priceImages([image])
    expect(prices[0]!.visualTokens).toBe(201)
  })

  it('builds handle and placeholder text through the supplied access resolution', () => {
    const access = { readonlyPath: '/world/attachments/photo.png' }
    const images = [ref('first', 800, 800), ref('second', 800, 800)]
    const prices = deepSeekImageRequestPricing(
      connection({ maxImagesPerRequest: 1, imageOffloadCountQuantum: 1 }),
      'vision',
      () => access,
    ).priceImages(images)
    expect(prices[0]).toEqual({ visualTokens: 0, text: offloadedImageText(images[0]!, access) })
    expect(prices[1]).toEqual({
      visualTokens: 349,
      text: requestImageHandleText(images[1]!, { width: 800, height: 800 }, access),
    })
    expect(prices[1]?.text).toContain('/world/attachments/photo.png')
  })

  it('prices count-offloaded oldest occurrences as their placeholder text', () => {
    const images = [ref('first', 800, 800), ref('second', 800, 800), ref('third', 800, 800)]
    const prices = deepSeekImageRequestPricing(
      connection({ maxImagesPerRequest: 2, imageOffloadCountQuantum: 1 }),
      'vision',
    ).priceImages(images)
    expect(prices).toEqual([
      { visualTokens: 0, text: offloadedImageText(images[0]!) },
      { visualTokens: 349, text: requestImageHandleText(images[1]!, { width: 800, height: 800 }) },
      { visualTokens: 349, text: requestImageHandleText(images[2]!, { width: 800, height: 800 }) },
    ])
  })

  it('caps each occurrence at the per-image byte target before the byte budget', () => {
    // Each 5 MiB source counts as the 1 MiB request target, so a 2 MiB budget
    // with a one-byte quantum removes exactly the oldest occurrence.
    const oversized = 5 * 1024 * 1024
    const images = [
      ref('first', 800, 800, oversized),
      ref('second', 800, 800, oversized),
      ref('third', 800, 800, oversized),
    ]
    const prices = deepSeekImageRequestPricing(
      connection({ maxRequestFilesBytes: 2 * 1024 * 1024, imageOffloadByteQuantum: 1 }),
      'vision',
    ).priceImages(images)
    expect(prices.map(price => price.visualTokens)).toEqual([0, 349, 349])
    expect(prices[0]!.text).toBe(offloadedImageText(images[0]!))
  })
})

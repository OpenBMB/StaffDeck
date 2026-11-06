/** Deterministic cached image versions for model requests. */

import { createHash, randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import sharp, { type Sharp } from 'sharp'
import { AttachmentError, ImageVariantId, requestImageDimensions } from '@deepseek-ai/dsh-attachment'
import type {
  ImageMediaType,
  ImageAttachmentRef,
  ImageRequestPolicy,
  RequestImageAttachment,
  StoredImageAttachment,
} from '@deepseek-ai/dsh-attachment'
import {
  IMAGE_ENCODING_QUALITIES,
  WEBP_ENCODING_EFFORT,
  encodeFirstWithinLimit,
  encodingLadder,
  isExhaustedEncoding,
} from './encoding.ts'
import { detectImage, encodedAlphaIsCompatible, probeImage } from './image.ts'

/** Transform version included in every cache and upload-index identity. */
export const REQUEST_IMAGE_TRANSFORM_VERSION = 'request-image-v5'

interface EncodedRequestImage {
  data: Uint8Array
  mediaType: ImageMediaType
  width: number
  height: number
}

interface VerifiedRequestImage extends EncodedRequestImage {
  hasAlpha: boolean
}

function digest(value: string | Uint8Array): string {
  return createHash('sha256').update(value).digest('hex')
}

function checkedInteger(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new AttachmentError(`${name} must be a positive integer.`, 'INVALID_ATTACHMENT_REF')
  }
  return value
}

function validatePolicy(policy: ImageRequestPolicy): void {
  checkedInteger(policy.maxPixels, 'Image request maxPixels')
  checkedInteger(policy.maxBytes, 'Image request maxBytes')
}

function descriptor(attachment: ImageAttachmentRef, policy: ImageRequestPolicy): string {
  return JSON.stringify({
    transformVersion: REQUEST_IMAGE_TRANSFORM_VERSION,
    attachmentId: attachment.attachmentId,
    routePixelBudget: policy.maxPixels,
    encodedByteBudget: policy.maxBytes,
    encoding: {
      webpQualities: IMAGE_ENCODING_QUALITIES,
      webpEffort: WEBP_ENCODING_EFFORT,
      jpegQualities: IMAGE_ENCODING_QUALITIES,
      order: ['alpha:webp', 'opaque:jpeg'],
      colourspace: 'srgb',
    },
  })
}

/**
 * Complete deterministic identity for one attachment and route-owned request policy.
 * @param attachment - provider-independent durable normalized attachment reference.
 * @param policy - route-owned pixel and byte policy.
 * @returns branded digest over every request transform input.
 */
export function requestImageVariantId(
  attachment: ImageAttachmentRef,
  policy: ImageRequestPolicy,
): ReturnType<typeof ImageVariantId> {
  return ImageVariantId(`sha256:${digest(descriptor(attachment, policy))}`)
}

function pipeline(attachment: StoredImageAttachment, width: number, height: number): Sharp {
  return sourcePipeline(attachment)
    .resize({ width, height, fit: 'inside', withoutEnlargement: true })
}

function sourcePipeline(attachment: StoredImageAttachment): Sharp {
  return sharp(attachment.data, { failOn: 'error', limitInputPixels: false }).toColourspace('srgb')
}

async function createRequestImage(
  attachment: StoredImageAttachment,
  policy: ImageRequestPolicy,
  hasAlpha: boolean,
): Promise<EncodedRequestImage> {
  const dimensions = requestImageDimensions(attachment.ref.width, attachment.ref.height, policy.maxPixels)
  if (dimensions.width === attachment.ref.width
    && dimensions.height === attachment.ref.height
    && attachment.data.byteLength <= policy.maxBytes) {
    return {
      data: attachment.data,
      mediaType: attachment.ref.mediaType,
      width: attachment.ref.width,
      height: attachment.ref.height,
    }
  }
  const encodedVersion = await encodeFirstWithinLimit(
    encodingLadder(pipeline(attachment, dimensions.width, dimensions.height), hasAlpha),
    policy.maxBytes,
  )
  return isExhaustedEncoding(encodedVersion) ? encodedVersion.smallest : encodedVersion
}

function cachePath(root: string, hash: string): string {
  return join(root, 'request-images', hash.slice(0, 2), hash)
}

async function readCached(
  path: string,
  attachment: StoredImageAttachment,
  policy: ImageRequestPolicy,
  expectedAlpha: boolean,
  signal?: AbortSignal,
): Promise<VerifiedRequestImage | undefined> {
  try {
    const data = new Uint8Array(await readFile(path, { signal }))
    const detected = await probeImage(data)
    const maximum = requestImageDimensions(attachment.ref.width, attachment.ref.height, policy.maxPixels)
    if (detected.depth !== 'uchar' || detected.space !== 'srgb'
      || detected.width > maximum.width || detected.height > maximum.height
      || !encodedAlphaIsCompatible(expectedAlpha, detected)) return undefined
    return { data, mediaType: detected.mediaType, width: detected.width, height: detected.height, hasAlpha: detected.hasAlpha }
  } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException | null)?.code === 'ENOENT') return undefined
    signal?.throwIfAborted()
    return undefined
  }
}

async function verifyRequestImage(
  image: EncodedRequestImage,
  expectedAlpha: boolean,
): Promise<VerifiedRequestImage> {
  const detected = await detectImage(image.data)
  if (detected.depth !== 'uchar' || detected.space !== 'srgb'
    || detected.width !== image.width || detected.height !== image.height
    || detected.mediaType !== image.mediaType || !encodedAlphaIsCompatible(expectedAlpha, detected)) {
    throw new AttachmentError(
      'Encoded model-request image does not match its verified 8-bit sRGB metadata.',
      'ATTACHMENT_WRITE_FAILED',
    )
  }
  return { ...image, hasAlpha: detected.hasAlpha }
}

async function writeCached(path: string, data: Uint8Array): Promise<void> {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 })
  const temporary = `${path}.${randomUUID()}.tmp`
  try {
    await writeFile(temporary, data, { mode: 0o600, flag: 'wx' })
    await rename(temporary, path)
  } finally {
    await rm(temporary, { force: true })
  }
}

/**
 * Generate or reuse one request image below the local attachment root.
 * @param root - absolute versioned attachment storage root.
 * @param attachment - verified normalized attachment bytes and reference.
 * @param policy - exact route request-image policy.
 * @param signal - optional cancellation for cache I/O and image transformation.
 * @returns verified request bytes and deterministic variant identity.
 */
export async function readRequestImageFile(
  root: string,
  attachment: StoredImageAttachment,
  policy: ImageRequestPolicy,
  signal?: AbortSignal,
): Promise<RequestImageAttachment> {
  signal?.throwIfAborted()
  validatePolicy(policy)
  const source = await probeImage(attachment.data)
  const variantId = requestImageVariantId(attachment.ref, policy)
  const hash = String(variantId).slice('sha256:'.length)
  const path = cachePath(root, hash)
  const cached = await readCached(path, attachment, policy, source.hasAlpha, signal)
  const created = cached ?? await createRequestImage(attachment, policy, source.hasAlpha)
  const version = cached ?? (created.data === attachment.data
    ? { ...created, hasAlpha: source.hasAlpha }
    : await verifyRequestImage(created, source.hasAlpha))
  signal?.throwIfAborted()
  if (cached === undefined && version.data !== attachment.data) await writeCached(path, version.data)
  return {
    variantId,
    attachment: attachment.ref,
    data: version.data,
    mediaType: version.mediaType,
    bytes: version.data.byteLength,
    width: version.width,
    height: version.height,
    depth: 'uchar',
    space: 'srgb',
    hasAlpha: version.hasAlpha,
  }
}

import "server-only";

import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
  timingSafeEqual,
} from "crypto";

export const META_CREDENTIAL_AAD = "quran-reels/meta-credentials/v1";
export const META_OAUTH_STATE_COOKIE = "qr_meta_oauth_state";

export interface MetaCredentialPayload {
  schema_version: 1;
  connected_at: string;
  graph_api_version: string;
  user_id: string;
  page_id: string;
  page_name: string;
  page_access_token: string;
  scopes: string[];
  token_expires_at?: string | null;
  instagram?: {
    id: string;
    username?: string | null;
    name?: string | null;
  } | null;
}

export interface EncryptedMetaCredentials {
  format: "aes-256-gcm";
  version: 1;
  iv: string;
  ciphertext: string;
  tag: string;
  created_at: string;
}

function getEncryptionKey(raw = process.env.META_CREDENTIAL_ENCRYPTION_KEY): Buffer {
  const value = raw?.trim() || "";
  if (!/^[A-Za-z0-9+/_-]+={0,2}$/.test(value)) {
    throw new Error("META_CREDENTIAL_ENCRYPTION_KEY must be a base64-encoded 32-byte key.");
  }
  const key = Buffer.from(value.replace(/-/g, "+").replace(/_/g, "/"), "base64");
  if (key.length !== 32) {
    throw new Error("META_CREDENTIAL_ENCRYPTION_KEY must decode to exactly 32 bytes.");
  }
  return key;
}

function encode(value: Buffer): string {
  return value.toString("base64url");
}

function decode(value: string): Buffer {
  return Buffer.from(value, "base64url");
}

export function encryptMetaCredentials(
  payload: MetaCredentialPayload,
  rawKey?: string
): EncryptedMetaCredentials {
  const key = getEncryptionKey(rawKey);
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from(META_CREDENTIAL_AAD, "utf-8"));
  const ciphertext = Buffer.concat([
    cipher.update(JSON.stringify(payload), "utf-8"),
    cipher.final(),
  ]);
  return {
    format: "aes-256-gcm",
    version: 1,
    iv: encode(iv),
    ciphertext: encode(ciphertext),
    tag: encode(cipher.getAuthTag()),
    created_at: new Date().toISOString(),
  };
}

export function decryptMetaCredentials(
  envelope: EncryptedMetaCredentials,
  rawKey?: string
): MetaCredentialPayload {
  if (envelope.format !== "aes-256-gcm" || envelope.version !== 1) {
    throw new Error("Unsupported encrypted Meta credential format.");
  }
  const decipher = createDecipheriv("aes-256-gcm", getEncryptionKey(rawKey), decode(envelope.iv));
  decipher.setAAD(Buffer.from(META_CREDENTIAL_AAD, "utf-8"));
  decipher.setAuthTag(decode(envelope.tag));
  const plaintext = Buffer.concat([
    decipher.update(decode(envelope.ciphertext)),
    decipher.final(),
  ]).toString("utf-8");
  const parsed = JSON.parse(plaintext) as MetaCredentialPayload;
  if (
    parsed.schema_version !== 1 ||
    !parsed.page_id ||
    !parsed.page_access_token ||
    !parsed.user_id
  ) {
    throw new Error("Decrypted Meta credentials are malformed.");
  }
  return parsed;
}

export function createOAuthState(): string {
  return randomBytes(32).toString("base64url");
}

export function verifyOAuthState(provided: string, cookieValue: string): boolean {
  if (!provided || !cookieValue) return false;
  const left = Buffer.from(provided, "utf-8");
  const right = Buffer.from(cookieValue, "utf-8");
  return left.length === right.length && timingSafeEqual(left, right);
}

export function safeMetaCredentialStatus(payload: MetaCredentialPayload) {
  return {
    connected: true as const,
    connectedAt: payload.connected_at,
    graphApiVersion: payload.graph_api_version,
    page: { id: payload.page_id, name: payload.page_name },
    instagram: payload.instagram
      ? { id: payload.instagram.id, username: payload.instagram.username || null, name: payload.instagram.name || null }
      : null,
    scopes: [...payload.scopes],
    tokenExpiresAt: payload.token_expires_at || null,
  };
}

import "server-only";

import bcrypt from "bcryptjs";
import { SignJWT, jwtVerify } from "jose";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export const SESSION_COOKIE_NAME = "qr_admin_session";
const SESSION_DURATION_SECONDS = 86400; // 24 hours

function getSessionSecretKey(): Uint8Array {
  const secret = process.env.SESSION_SECRET;
  if (!secret || secret.trim().length === 0) {
    throw new Error("SESSION_SECRET is not configured in server environment.");
  }
  return new TextEncoder().encode(secret.trim());
}

/**
 * Verify a plaintext password against the stored bcrypt ADMIN_PASSWORD_HASH.
 */
export async function verifyAdminPassword(plaintext: string): Promise<boolean> {
  const hash = process.env.ADMIN_PASSWORD_HASH;
  if (!hash || hash.trim().length === 0) {
    console.error("[AUTH] ADMIN_PASSWORD_HASH is not configured.");
    return false;
  }
  if (!plaintext || typeof plaintext !== "string") {
    return false;
  }
  try {
    return await bcrypt.compare(plaintext, hash.trim());
  } catch (err) {
    console.error("[AUTH] Password comparison error:", err);
    return false;
  }
}

/**
 * Create a signed, stateless JWT session token.
 */
export async function createSessionToken(): Promise<string> {
  const secretKey = getSessionSecretKey();
  return await new SignJWT({ role: "admin" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime(`${SESSION_DURATION_SECONDS}s`)
    .sign(secretKey);
}

/**
 * Verify a signed session token.
 */
export async function verifySessionToken(token: string): Promise<boolean> {
  if (!token || typeof token !== "string") {
    return false;
  }
  try {
    const secretKey = getSessionSecretKey();
    const { payload } = await jwtVerify(token, secretKey, {
      algorithms: ["HS256"],
    });
    return payload.role === "admin";
  } catch {
    return false;
  }
}

/**
 * Check if the incoming request has a valid admin session cookie.
 */
export async function getAdminSession(): Promise<boolean> {
  try {
    const cookieStore = await cookies();
    const sessionCookie = cookieStore.get(SESSION_COOKIE_NAME);
    if (!sessionCookie || !sessionCookie.value) {
      return false;
    }
    return await verifySessionToken(sessionCookie.value);
  } catch {
    return false;
  }
}

/**
 * Require admin session or redirect to /login.
 */
export async function requireAdminSession(): Promise<void> {
  const isAuthenticated = await getAdminSession();
  if (!isAuthenticated) {
    redirect("/login");
  }
}

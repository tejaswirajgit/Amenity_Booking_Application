import { NextResponse } from 'next/server';

const defaultBackendBaseUrl = 'http://127.0.0.1:8000';

function toProxyErrorResponse(error: unknown, fallbackMessage: string) {
  const backendBaseUrl = getBackendBaseUrl();
  const message = error instanceof Error ? error.message : fallbackMessage;
  const isConnectivityError = message.toLowerCase().includes('fetch failed');

  return NextResponse.json(
    {
      detail: isConnectivityError
        ? `Admin frontend could not reach backend at ${backendBaseUrl}. Start the FastAPI server or update ADMIN_API_BASE_URL in frontend/admin/.env.local.`
        : message,
    },
    { status: isConnectivityError ? 502 : 500 },
  );
}

function getBackendBaseUrl() {
  return (process.env.ADMIN_API_BASE_URL?.trim() || defaultBackendBaseUrl).replace(/\/+$/, '');
}

function getAdminApiKey() {
  return process.env.ADMIN_API_KEY?.trim() || '';
}

async function proxyToBackend(method: 'GET' | 'POST', body?: string) {
  const adminApiKey = getAdminApiKey();
  if (!adminApiKey) {
    return NextResponse.json(
      { detail: 'Missing ADMIN_API_KEY for the admin frontend. Add it to frontend/admin/.env.local.' },
      { status: 500 },
    );
  }

  const response = await fetch(`${getBackendBaseUrl()}/v1/admin/users`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-API-Key': adminApiKey,
    },
    body,
    cache: 'no-store',
  });

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = { detail: 'Backend did not return JSON.' };
  }

  return NextResponse.json(payload, { status: response.status });
}

export async function GET() {
  try {
    return await proxyToBackend('GET');
  } catch (error) {
    return toProxyErrorResponse(error, 'Unable to load users from backend.');
  }
}

export async function POST(request: Request) {
  try {
    return await proxyToBackend('POST', await request.text());
  } catch (error) {
    return toProxyErrorResponse(error, 'Unable to create user.');
  }
}
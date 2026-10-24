import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { phoneJidOf } from '../src/baileys.js';

describe('phoneJidOf', () => {
  it('strips the device suffix from a phone-number JID', () => {
    assert.equal(phoneJidOf({ id: '51999111222:12@s.whatsapp.net' }), '51999111222@s.whatsapp.net');
  });

  it('falls back to phoneNumber for a LID id', () => {
    assert.equal(
      phoneJidOf({ id: '123456789:3@lid', phoneNumber: '51999111222:3@s.whatsapp.net' }),
      '51999111222@s.whatsapp.net',
    );
  });

  it('returns undefined for a LID id without phoneNumber, or no user', () => {
    assert.equal(phoneJidOf({ id: '123456789@lid' }), undefined);
    assert.equal(phoneJidOf(undefined), undefined);
  });
});

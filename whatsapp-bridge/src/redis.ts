// Redis pub/sub bus between the bridge and the Python orchestrator
// (SPEC.md §4.5, §6). Publishes qualifying WhatsApp messages to
// `whatsapp:incoming`; listens on `whatsapp:outgoing` for replies to send.

import { Redis } from 'ioredis';
import type { BridgeConfig } from './config.js';

export type IncomingType = 'text' | 'voice' | 'attachment';

export interface IncomingEvent {
  message_id: string;
  timestamp: string;
  sender_jid: string;
  type: IncomingType;
  text: string | null;
  media_path: string | null;
  media_mime_type: string | null;
}

export interface OutgoingEvent {
  text: string;
  in_reply_to: string | null;
}

export class RedisBus {
  private readonly publisher: Redis;
  private readonly subscriber: Redis;
  private readonly config: BridgeConfig;

  constructor(config: BridgeConfig) {
    this.config = config;
    this.publisher = new Redis({ host: config.redisHost, port: config.redisPort });
    this.subscriber = new Redis({ host: config.redisHost, port: config.redisPort });
  }

  async publishIncoming(event: IncomingEvent): Promise<void> {
    await this.publisher.publish(this.config.incomingChannel, JSON.stringify(event));
  }

  async onOutgoing(handler: (event: OutgoingEvent) => void | Promise<void>): Promise<void> {
    await this.subscriber.subscribe(this.config.outgoingChannel);
    this.subscriber.on('message', (channel: string, message: string) => {
      if (channel !== this.config.outgoingChannel) return;
      let event: OutgoingEvent;
      try {
        event = JSON.parse(message) as OutgoingEvent;
      } catch {
        return;
      }
      void handler(event);
    });
  }

  async close(): Promise<void> {
    await this.publisher.quit();
    await this.subscriber.quit();
  }
}

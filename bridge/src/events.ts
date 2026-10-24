// Event shapes exchanged with the orchestrator over the socket bus.

export type IncomingType = 'text' | 'voice' | 'attachment';

export interface IncomingEvent {
  message_id: string;
  timestamp: string;
  sender_jid: string;
  type: IncomingType;
  text: string | null;
  media_path: string | null;
  media_mime_type: string | null;
  // WhatsApp's reply/quote feature (SPEC.md §5.9/§7.6) — set regardless of
  // this message's own `type`. quoted_text is set verbatim for a quoted text
  // message; quoted_media_path/mime_type are set when a quoted
  // attachment/voice note was successfully re-downloaded;
  // quoted_media_type_label describes the quoted content either way (used
  // as the fallback description when the download itself failed).
  quoted_text: string | null;
  quoted_media_path: string | null;
  quoted_media_mime_type: string | null;
  quoted_media_type_label: string | null;
}

export interface OutgoingEvent {
  // Exactly one of text/media_path is set per event (SPEC.md §7.4/§7.5).
  text: string | null;
  in_reply_to: string | null;
  media_path: string | null;
  media_mime_type: string | null;
  media_filename: string | null;
  caption: string | null;
  delete_after_send: boolean;
}

#!/usr/bin/env python3
"""Puente temporal de WhatsApp para la prueba privada de Daniela."""

import hashlib
import hmac
import json
import re
import secrets
import sys
from collections import OrderedDict
from getpass import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Full, Queue
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

REPLY = "Hola, soy el asistente de prueba de Daniela. ¡WhatsApp ya está conectado!"
DIFY_CHAT_URL = "http://127.0.0.1:8080/v1/chat-messages"
DIFY_FALLBACK = "De momento, não consegui obter uma resposta. Podes tentar novamente?"


class Bridge(HTTPServer):
    def __init__(self, app_secret, access_token, phone_id, version, allowed, dify_key=""):
        super().__init__(("127.0.0.1", 8000), Webhook)
        self.app_secret = app_secret.encode()
        self.verify_token = secrets.token_urlsafe(24)
        self.access_token = access_token
        self.phone_id = phone_id
        self.allowed = allowed
        self.dify_key = dify_key
        self.conversations = {}
        self.send_url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
        self.queue = Queue(maxsize=64)
        self.seen = OrderedDict()

    def ask_dify(self, sender, question):
        user_id = hmac.new(self.app_secret, sender.encode(), hashlib.sha256).hexdigest()
        payload = {"inputs": {}, "query": question, "response_mode": "blocking",
                   "user": f"whatsapp-{user_id}"}
        if sender in self.conversations:
            payload["conversation_id"] = self.conversations[sender]
        request = Request(DIFY_CHAT_URL, data=json.dumps(payload).encode(),
                          headers={"Authorization": f"Bearer {self.dify_key}",
                                   "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=45) as response:
                result = json.load(response)
            answer = result["answer"].strip()
            if not answer:
                raise ValueError("Respuesta vacía")
        except HTTPError as error:
            print(f"Dify rechazó la consulta (HTTP {error.code}).", flush=True)
            return DIFY_FALLBACK
        except (URLError, OSError, ValueError, TypeError, KeyError, AttributeError):
            print("Dify no devolvió una respuesta válida.", flush=True)
            return DIFY_FALLBACK

        conversation_id = result.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            self.conversations[sender] = conversation_id
        print("Respuesta recibida de Dify.", flush=True)
        return answer if len(answer) <= 2000 else answer[:1999].rstrip() + "…"

    def send_replies(self):
        while True:
            sender, question = self.queue.get()
            answer = self.ask_dify(sender, question) if self.dify_key else REPLY
            payload = {"messaging_product": "whatsapp", "to": sender,
                       "type": "text", "text": {"body": answer}}
            request = Request(self.send_url, data=json.dumps(payload).encode(),
                              headers={"Authorization": f"Bearer {self.access_token}",
                                       "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=15) as response:
                    print(f"Respuesta aceptada por Meta (HTTP {response.status}).", flush=True)
            except HTTPError as error:
                try:
                    code = json.loads(error.read(4096)).get("error", {}).get("code")
                except (ValueError, AttributeError):
                    code = None
                print(f"Meta rechazó el envío (HTTP {error.code}, código {code}).", flush=True)
            except (URLError, OSError):
                print("No se pudo conectar con la API de Meta.", flush=True)
            finally:
                self.queue.task_done()


class Webhook(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_args):
        # La URL de verificación contiene el token. Evita registrarla.
        pass

    def reply(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/health":
            return self.reply(200, b"OK")
        if url.path != "/webhook":
            return self.reply(404)

        args = parse_qs(url.query)
        token = args.get("hub.verify_token", [""])[0].encode()
        challenge = args.get("hub.challenge", [""])[0]
        if (args.get("hub.mode") == ["subscribe"] and challenge
                and hmac.compare_digest(token, self.server.verify_token.encode())):
            return self.reply(200, challenge.encode())
        self.reply(403)

    def do_POST(self):
        if urlsplit(self.path).path != "/webhook":
            return self.reply(404)

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.reply(400)
        if not 0 < length <= 1024 * 1024:
            return self.reply(413)

        body = self.rfile.read(length)
        expected = b"sha256=" + hmac.new(
            self.server.app_secret, body, hashlib.sha256
        ).hexdigest().encode()
        signature = self.headers.get("X-Hub-Signature-256", "").encode()
        if not hmac.compare_digest(expected, signature):
            return self.reply(403)

        try:
            event = json.loads(body)
            self.enqueue_messages(event)
        except (ValueError, TypeError, AttributeError):
            return self.reply(400)
        except Full:
            return self.reply(503)
        self.reply(200)

    def enqueue_messages(self, event):
        for entry in event.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if (change.get("field") != "messages"
                        or value.get("metadata", {}).get("phone_number_id") != self.server.phone_id):
                    continue

                for message in value.get("messages", []):
                    sender = message.get("from")
                    message_id = message.get("id")
                    if (message.get("type") != "text" or sender not in self.server.allowed
                            or not message_id or message_id in self.server.seen):
                        continue

                    question = message.get("text", {}).get("body", "").strip()
                    if not question:
                        continue
                    self.server.queue.put_nowait((sender, question))
                    self.server.seen[message_id] = None
                    if len(self.server.seen) > 1024:
                        self.server.seen.popitem(last=False)


def main():
    if not sys.stdin.isatty():
        raise SystemExit("Abre Terminal y ejecuta python3 puente_whatsapp.py.")
    phone_id = input("Phone number ID del número de prueba: ").strip()
    version = input("Versión de Graph API del ejemplo de Meta (vNN.0): ").strip()
    numbers = input("Móviles autorizados, con prefijo de país y separados por comas: ")
    allowed = {re.sub(r"[+\s-]", "", number) for number in numbers.split(",")}
    if not re.fullmatch(r"[0-9]+", phone_id) or not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
        raise SystemExit("Revisa el Phone number ID y la versión de Graph API.")
    if not all(re.fullmatch(r"[1-9][0-9]{6,14}", number) for number in allowed):
        raise SystemExit("Introduce los móviles con prefijo internacional.")

    app_secret = getpass("App Secret de la app de Meta: ").strip()
    access_token = getpass("Access Token temporal de WhatsApp: ").strip()
    if not app_secret or not access_token:
        raise SystemExit("El App Secret y el Access Token son obligatorios.")

    dify_key = getpass("Clave API de Dify (Enter para probar el saludo fijo): ").strip()
    with Bridge(app_secret, access_token, phone_id, version, allowed, dify_key) as server:
        print("Modo Dify por SSH." if dify_key else "Modo saludo fijo.")
        print(f"Verify token para el formulario de Meta: {server.verify_token}")
        print("Escuchando en http://127.0.0.1:8000. Ctrl+C para terminar.", flush=True)
        Thread(target=server.send_replies, daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

"""Fake clock + fake Playwright page, so browser code runs instantly and deterministically."""
from tests.conftest import png_bytes, solid


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.t = start

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class Keyboard:
    def __init__(self, page=None):
        self.pressed, self.page = [], page

    def press(self, k):
        self.pressed.append(k)
        if self.page and self.page.advance_on in ("key", "any"):
            self.page.advance()


class Mouse:
    def __init__(self, page=None):
        self.wheels, self.page = 0, page

    def wheel(self, x, y):
        self.wheels += 1
        if self.page and self.page.advance_on in ("wheel", "any"):
            self.page.advance()

    def move(self, x, y):
        pass

    def click(self, x, y):
        pass


class FakePage:
    def __init__(self, clock=None, url="https://www.instagram.com/reels/", body="", duration=15.0,
                 og_desc="", on_goto=None, links=(), image=None):
        self.clock, self.url, self.body, self.duration = clock, url, body, duration
        self.og_desc, self.on_goto, self.links = og_desc, on_goto, list(links)
        self.keyboard, self.mouse = Keyboard(self), Mouse(self)
        self.queue = []            # urls the feed advances to on scroll
        self.advance_on = "any"    # "key" | "wheel" | "any" | "never"
        self.screens, self.likes, self.gotos = 0, 0, []
        self.image = image or solid((200, 120, 40))
        self.video = True

    def advance(self):
        if self.queue:
            self.url = self.queue.pop(0)

    def evaluate(self, js):
        if "application/json" in js:
            return list(getattr(self, "embedded", []))
        if "svg[aria-label=\"Like\"]" in js:
            self.likes += 1
            return True
        if not self.video:
            return None
        return {"x": 0, "y": 0, "w": 270, "h": 480, "duration": self.duration, "t": 0, "href": None}

    def screenshot(self, clip=None):
        self.screens += 1
        return png_bytes(self.image)

    def inner_text(self, sel, timeout=None):
        return self.body

    def get_attribute(self, sel, attr, timeout=None):
        return self.og_desc

    def goto(self, url, wait_until=None, timeout=None, **kwargs):
        self.url = url
        self.gotos.append(url)
        if self.on_goto:
            self.on_goto(self, url)

    def wait_for_timeout(self, ms):
        if self.clock:
            self.clock.sleep(ms / 1000)

    def eval_on_selector_all(self, sel, js):
        return list(self.links)

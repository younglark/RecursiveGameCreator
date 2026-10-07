"""Offline role-image check, run as the ordinary runner user."""
from playwright.sync_api import sync_playwright
from PIL import Image

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
    page = browser.new_page(viewport={"width": 960, "height": 540})
    page.set_content("""<canvas id="scene"></canvas><button id="button">Check</button>
    <script>
    window.clicks = 0; window.dragMoves = 0;
    button.onclick = () => clicks++;
    document.onmousemove = event => { if (event.buttons === 1) dragMoves++; };
    </script>""")
    assert page.locator("button").inner_text() == "Check"
    page.locator("button").click()
    assert page.evaluate("clicks") == 1
    assert page.evaluate("Boolean(scene.getContext('webgl2') || scene.getContext('webgl'))")
    page.mouse.move(50, 50)
    page.mouse.down()
    page.mouse.move(100, 100, steps=3)
    page.mouse.up()
    assert page.evaluate("dragMoves") >= 3
    browser.close()
print("Role browser tools OK: HTML, WebGL, pointer drag, Pillow", Image.__version__)

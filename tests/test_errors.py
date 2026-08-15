"""错误页：404/405 渲染统一中文外壳，不再抛框架裸报错。"""


def test_404_renders_styled_page(client):
    resp = client.get("/no-such-page-xyz")
    assert resp.status_code == 404
    body = resp.get_data(as_text=True)
    assert "页面不存在" in body
    # 继承 base.html 外壳，而不是 gunicorn 裸报错
    assert "门店日报" in body
    assert "404" in body


def test_405_renders_styled_page(client):
    # /report 只允许 GET/HEAD，POST 触发 405（TESTING 下跳过 CSRF）
    resp = client.post("/report")
    assert resp.status_code == 405
    assert "方法不支持" in resp.get_data(as_text=True)

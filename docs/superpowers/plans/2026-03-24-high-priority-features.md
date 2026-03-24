# High-Priority Soft UA Features — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 7 high-priority softphone features to the PJSUA MCP server — auto-answer, reject call, call transfer (blind + attended), SIP MESSAGE, conference calling, and call history — each with integration tests using two or three MCP UA instances via Docker Compose + Asterisk.

**Architecture:** Each feature adds a method to `CallManager` (or `AccountManager` for messaging), a corresponding MCP tool in `server.py`, and an integration test in `test_integration.py` that exercises the feature across multiple UA instances. Asterisk config is extended with a third extension (6003) for transfer/conference tests.

**Tech Stack:** Python 3.13, PJSUA2 (pjproject 2.14.1), FastMCP, pytest, Docker Compose, Asterisk 22 (andrius/asterisk)

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/call_manager.py` | Add `reject_call()`, `transfer_blind()`, `transfer_attended()`, `conference()` methods |
| `src/account_manager.py` | Add `onInstantMessage` callback, `send_message()`, incoming message queue |
| `src/server.py` | Add 7 new MCP tools: `reject_call`, `blind_transfer`, `attended_transfer`, `send_message`, `get_messages`, `conference`, `get_call_history` |
| `tests/test_integration.py` | Add `TestRejectCall`, `TestAutoAnswer`, `TestBlindTransfer`, `TestAttendedTransfer`, `TestSipMessage`, `TestConference`, `TestCallHistory` classes |
| `tests/test_call_manager.py` | Unit tests for new CallManager guards |
| `tests/test_account_manager.py` | Unit tests for message queue |
| `tests/asterisk/pjsip.conf` | Add extension 6003 for 3-party scenarios |
| `tests/asterisk/extensions.conf` | Add 6003 routing |
| `docker-compose.test.yml` | Add `SIP_USER_C` / `SIP_PASS_C` env vars |

---

## Task 1: Auto-Answer

Currently `answer_call` must be called manually. Add an `auto_answer` flag to `configure` that automatically answers incoming calls with 200 OK.

### Files:
- Modify: `src/account_manager.py` — add `_auto_answer` flag, call `answer()` in `onIncomingCall`
- Modify: `src/server.py:90-151` — add `auto_answer` param to `configure` tool
- Modify: `src/call_manager.py:237-246` — handle auto-answered calls in `_on_incoming_call`
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# In tests/test_integration.py, add to TestCallFlow or new class:

def test_auto_answer(self):
    """Callee with auto_answer=True answers automatically."""
    # Register caller normally
    _configure_and_register(self.caller, SIP_USER_A, SIP_PASS_A)
    # Register callee with auto_answer
    _parse_tool_result(self.callee.call_tool("configure", {
        "domain": SIP_DOMAIN, "transport": "udp",
        "username": SIP_USER_B, "password": SIP_PASS_B,
        "auto_answer": True,
    }))
    _parse_tool_result(self.callee.call_tool("register"))
    _wait_registered(self.callee)

    # Caller dials callee — should auto-answer
    result = _parse_tool_result(self.caller.call_tool("make_call", {
        "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
    }))
    call_id = result["call_id"]

    # Wait for CONFIRMED (no manual answer_call needed)
    time.sleep(3)
    caller_info = _parse_tool_result(
        self.caller.call_tool("get_call_info", {"call_id": call_id})
    )
    assert caller_info["state"] == "CONFIRMED"

    self.caller.call_tool("hangup", {"call_id": call_id})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/test_integration.py::TestCallFlow::test_auto_answer -v -x`
Expected: FAIL — `configure` doesn't accept `auto_answer` param

- [ ] **Step 3: Implement auto_answer in AccountManager**

In `src/account_manager.py`, add `_auto_answer` field:

```python
# In AccountManager.__init__:
self._auto_answer: bool = False

# Add public property:
@property
def auto_answer(self) -> bool:
    return self._auto_answer

# In AccountManager.configure():
def configure(self, domain, username=None, password=None, realm=None, srtp=False, auto_answer=False):
    # ... existing code ...
    self._auto_answer = auto_answer

# In SipAccount.onIncomingCall():
def onIncomingCall(self, prm):
    log.info("Incoming call: call_id=%d", prm.callId)
    if self.on_incoming_call_cb:
        self.on_incoming_call_cb(prm.callId)
```

In `src/call_manager.py`, auto-answer in `_on_incoming_call`:

```python
def _on_incoming_call(self, call_id: int) -> None:
    acc = self._account_mgr.account
    if not acc:
        return
    call = SipCall(acc, call_id)
    with self._lock:
        self._calls[call_id] = call
        self._incoming_queue.append(call_id)
    log.info("Incoming call %d queued", call_id)

    # Auto-answer if configured
    if self._account_mgr.auto_answer:
        prm = pj.CallOpParam()
        prm.statusCode = 200
        call.answer(prm)
        log.info("Auto-answered call %d", call_id)
```

In `src/server.py`, add `auto_answer` param to `configure` tool signature and pass to `account_mgr.configure()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/test_integration.py::TestCallFlow::test_auto_answer -v -x`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `docker compose -f docker-compose.test.yml run --build --rm test-runner`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add src/account_manager.py src/call_manager.py src/server.py tests/test_integration.py
git commit -m "feat: add auto_answer flag to configure tool"
```

---

## Task 2: Reject Call

Allow rejecting incoming calls with a specific SIP status code (486 Busy, 603 Decline, etc.) instead of only accepting or ignoring.

### Files:
- Modify: `src/call_manager.py` — add `reject_call()` method
- Modify: `src/server.py` — add `reject_call` MCP tool
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
def test_reject_call(self):
    """Callee rejects incoming call with 486 Busy."""
    self._register_both()

    self.caller.call_tool("make_call", {
        "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
    })

    # Callee rejects
    deadline = time.time() + 5
    while time.time() < deadline:
        result = _parse_tool_result(self.callee.call_tool("reject_call", {
            "status_code": 486,
        }))
        if result.get("status") == "ok":
            break
        time.sleep(0.5)
    else:
        raise AssertionError("reject_call never succeeded")

    time.sleep(1)

    # Caller should see DISCONNECTED
    log = _parse_tool_result(self.caller.call_tool("get_sip_log", {
        "filter_text": "486",
    }))
    assert log["total_count"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement reject_call**

In `src/call_manager.py`:

```python
def reject_call(self, call_id: int | None = None, status_code: int = 486) -> dict[str, Any]:
    """Reject an incoming call with a SIP error code."""
    call = self._get_call(call_id, from_incoming=True)
    prm = pj.CallOpParam()
    prm.statusCode = status_code
    call.hangup(prm)
    return {"call_id": call_id, "status_code": status_code}
```

In `src/server.py`:

```python
@mcp.tool()
async def reject_call(call_id: int | None = None, status_code: int = 486) -> dict[str, Any]:
    """Reject an incoming call with a SIP error response.

    Args:
        call_id: Call ID to reject (default: first incoming call)
        status_code: SIP response code — 486 (Busy), 603 (Decline), 480 (Unavailable)
    """
    assert call_mgr is not None
    try:
        info = call_mgr.reject_call(call_id=call_id, status_code=status_code)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("reject_call failed")
        return {"status": "error", "error": str(e)}
```

- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Run full test suite**
- [ ] **Step 6: Commit**

```bash
git add src/call_manager.py src/server.py tests/test_integration.py
git commit -m "feat: add reject_call tool with configurable SIP status code"
```

---

## Task 3: Blind Transfer

Blind (unattended) transfer — UA A transfers an active call to UA C without consulting C first. Uses PJSUA2's `Call.xfer()` method which sends SIP REFER.

### Prerequisites:
- Add extension 6003 to Asterisk config
- Add `SIP_USER_C` / `SIP_PASS_C` to docker-compose.test.yml

### Files:
- Modify: `tests/asterisk/pjsip.conf` — add extension 6003
- Modify: `tests/asterisk/extensions.conf` — add 6003 routing
- Modify: `docker-compose.test.yml` — add SIP_USER_C env vars
- Modify: `src/call_manager.py` — add `blind_transfer()` method
- Modify: `src/server.py` — add `blind_transfer` MCP tool
- Test: `tests/test_integration.py`

- [ ] **Step 1: Add Asterisk extension 6003**

In `tests/asterisk/pjsip.conf`, append:

```ini
; --- Extension 6003 ---

[6003]
type=endpoint
context=testing
disallow=all
allow=ulaw
allow=alaw
auth=6003
aors=6003
direct_media=no

[6003]
type=auth
auth_type=userpass
username=6003
password=test123

[6003]
type=aor
max_contacts=1
remove_existing=yes
```

In `tests/asterisk/extensions.conf`:

```ini
[testing]
exten => 6001,1,Dial(PJSIP/6001,30)
exten => 6002,1,Dial(PJSIP/6002,30)
exten => 6003,1,Dial(PJSIP/6003,30)
```

In `docker-compose.test.yml`, add env vars:

```yaml
    environment:
      - SIP_USER_C=6003
      - SIP_PASS_C=test123
```

In `tests/test_integration.py`, add:

```python
SIP_USER_C = os.environ.get("SIP_USER_C", "6003")
SIP_PASS_C = os.environ.get("SIP_PASS_C", "test123")
```

- [ ] **Step 2: Write integration test**

```python
@skip_no_domain
class TestBlindTransfer:
    @pytest.fixture(autouse=True)
    def mcp_trio(self):
        with McpClient() as a, McpClient() as b, McpClient() as c:
            a.send_initialize()
            b.send_initialize()
            c.send_initialize()
            self.ua_a = a
            self.ua_b = b
            self.ua_c = c
            yield

    def _register_all(self):
        _configure_and_register(self.ua_a, SIP_USER_A, SIP_PASS_A)
        _configure_and_register(self.ua_b, SIP_USER_B, SIP_PASS_B)
        # C with auto_answer so transfer completes automatically
        _parse_tool_result(self.ua_c.call_tool("configure", {
            "domain": SIP_DOMAIN, "transport": "udp",
            "username": SIP_USER_C, "password": SIP_PASS_C,
            "auto_answer": True,
        }))
        _parse_tool_result(self.ua_c.call_tool("register"))
        _wait_registered(self.ua_c)

    def test_blind_transfer(self):
        """A calls B, then B blind-transfers A to C."""
        self._register_all()

        # A calls B
        result = _parse_tool_result(self.ua_a.call_tool("make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        assert result["status"] == "ok"
        self._wait_and_answer(self.ua_b)
        time.sleep(1)

        # B transfers A to C (blind)
        result = _parse_tool_result(self.ua_b.call_tool("blind_transfer", {
            "dest_uri": f"sip:{SIP_USER_C}@{SIP_DOMAIN}",
        }))
        assert result["status"] == "ok"

        # Wait for transfer to complete — C auto-answers
        time.sleep(3)

        # A should now be connected to C
        # Check SIP log on A for REFER/NOTIFY
        log_result = _parse_tool_result(self.ua_a.call_tool("get_sip_log", {
            "filter_text": "REFER",
        }))
        # REFER or NOTIFY with refer should appear
        # B should be disconnected after transfer
        time.sleep(1)
        b_log = _parse_tool_result(self.ua_b.call_tool("get_sip_log", {
            "filter_text": "BYE",
        }))
        assert b_log["total_count"] > 0

    @staticmethod
    def _wait_and_answer(client, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = _parse_tool_result(client.call_tool("answer_call"))
            if result.get("status") == "ok":
                return result
            time.sleep(0.5)
        raise AssertionError(f"No incoming call within {timeout}s")
```

- [ ] **Step 3: Implement blind_transfer**

In `src/call_manager.py`:

```python
def blind_transfer(self, dest_uri: str, call_id: int | None = None) -> dict[str, Any]:
    """Blind transfer: send REFER to redirect the call."""
    call = self._get_call(call_id)
    prm = pj.CallOpParam()
    call.xfer(dest_uri, prm)
    return {"call_id": call_id, "transfer_to": dest_uri}
```

In `src/server.py`:

```python
@mcp.tool()
async def blind_transfer(dest_uri: str, call_id: int | None = None) -> dict[str, Any]:
    """Blind transfer: redirect an active call to another SIP URI.

    The current call receives a SIP REFER, causing the remote party to
    send a new INVITE to dest_uri. Our side of the call is disconnected.

    Args:
        dest_uri: Transfer destination (e.g. "sip:6003@asterisk")
        call_id: Call ID to transfer (default: current active call)
    """
    assert call_mgr is not None
    try:
        info = call_mgr.blind_transfer(dest_uri, call_id=call_id)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("blind_transfer failed")
        return {"status": "error", "error": str(e)}
```

- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Run full test suite**
- [ ] **Step 6: Commit**

```bash
git add src/call_manager.py src/server.py tests/ docker-compose.test.yml
git commit -m "feat: add blind_transfer tool with REFER support"
```

---

## Task 4: Attended Transfer

Attended transfer — UA B has a call with A, calls C to consult, then transfers A to C replacing B's call with C. Uses `Call.xferReplaces()`.

### Files:
- Modify: `src/call_manager.py` — add `attended_transfer()` method
- Modify: `src/server.py` — add `attended_transfer` MCP tool
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
def test_attended_transfer(self):
    """A calls B, B consults C, then B transfers A to C."""
    self._register_all()

    # A calls B
    r_ab = _parse_tool_result(self.ua_a.call_tool("make_call", {
        "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
    }))
    b_answer = self._wait_and_answer(self.ua_b)
    ab_call_id_on_b = b_answer["call_id"]
    time.sleep(1)

    # B puts A on hold
    _parse_tool_result(self.ua_b.call_tool("hold", {
        "call_id": ab_call_id_on_b,
    }))
    time.sleep(0.5)

    # B calls C for consultation
    r_bc = _parse_tool_result(self.ua_b.call_tool("make_call", {
        "dest_uri": f"sip:{SIP_USER_C}@{SIP_DOMAIN}",
    }))
    bc_call_id_on_b = r_bc["call_id"]
    # C auto-answers
    time.sleep(2)

    # B transfers A to C (attended — replaces B-C call)
    result = _parse_tool_result(self.ua_b.call_tool("attended_transfer", {
        "call_id": ab_call_id_on_b,
        "dest_call_id": bc_call_id_on_b,
    }))
    assert result["status"] == "ok"

    time.sleep(3)

    # B should be disconnected from both calls
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement attended_transfer**

In `src/call_manager.py`:

```python
def attended_transfer(self, call_id: int | None = None, dest_call_id: int | None = None) -> dict[str, Any]:
    """Attended transfer: connect two calls, removing ourselves.

    call_id (or current active) is transferred to dest_call_id (second active call).
    Uses REFER with Replaces header.
    """
    with self._lock:
        active_calls = [
            (cid, call) for cid, call in self._calls.items()
            if call.isActive()
        ]
    if len(active_calls) < 2:
        raise RuntimeError("Need at least 2 active calls for attended transfer")

    if call_id is not None and dest_call_id is not None:
        src_call = self._get_call_by_id(call_id)
        dst_call = self._get_call_by_id(dest_call_id)
    else:
        # Auto-select: first two active calls
        src_call = active_calls[0][1]
        dst_call = active_calls[1][1]

    prm = pj.CallOpParam()
    src_call.xferReplaces(dst_call, prm)
    return {"status": "transferred"}
```

In `src/server.py`:

```python
@mcp.tool()
async def attended_transfer(
    call_id: int | None = None,
    dest_call_id: int | None = None,
) -> dict[str, Any]:
    """Attended transfer: bridge two active calls and disconnect ourselves.

    Must have two active calls (e.g. original call on hold + consultation call).
    Sends REFER with Replaces header to connect the two remote parties directly.

    Args:
        call_id: Source call to transfer (default: auto-select first active)
        dest_call_id: Destination call to replace (default: auto-select second active)
    """
    assert call_mgr is not None
    try:
        info = call_mgr.attended_transfer(call_id=call_id, dest_call_id=dest_call_id)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("attended_transfer failed")
        return {"status": "error", "error": str(e)}
```

- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Run full test suite**
- [ ] **Step 6: Commit**

```bash
git add src/call_manager.py src/server.py tests/test_integration.py
git commit -m "feat: add attended_transfer tool with REFER/Replaces"
```

---

## Task 5: SIP MESSAGE (Instant Messaging)

Send and receive text messages via SIP MESSAGE method. Useful for AI → human text communication alongside voice.

### Files:
- Modify: `src/account_manager.py` — add `onInstantMessage` callback, message queue, `send_message()` method
- Modify: `src/server.py` — add `send_message` and `get_messages` MCP tools
- Modify: `tests/test_account_manager.py` — unit test for message queue
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
@skip_no_domain
class TestSipMessage:
    @pytest.fixture(autouse=True)
    def mcp_pair(self):
        with McpClient() as a, McpClient() as b:
            a.send_initialize()
            b.send_initialize()
            self.ua_a = a
            self.ua_b = b
            yield

    def test_send_and_receive_message(self):
        _configure_and_register(self.ua_a, SIP_USER_A, SIP_PASS_A)
        _configure_and_register(self.ua_b, SIP_USER_B, SIP_PASS_B)

        # A sends message to B
        result = _parse_tool_result(self.ua_a.call_tool("send_message", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
            "body": "Hello from A",
        }))
        assert result["status"] == "ok"

        time.sleep(1)

        # B checks received messages
        result = _parse_tool_result(self.ua_b.call_tool("get_messages"))
        assert result["total_count"] > 0
        assert any("Hello from A" in m["body"] for m in result["messages"])
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement SIP MESSAGE support**

In `src/account_manager.py`, add to `SipAccount`:

```python
def __init__(self):
    super().__init__()
    # ... existing ...
    self._messages: deque[dict] = deque(maxlen=100)

def onInstantMessage(self, prm):
    msg = {
        "from": prm.fromUri,
        "to": prm.toUri,
        "body": prm.msgBody,
        "content_type": prm.contentType,
        "timestamp": datetime.now().isoformat(),
    }
    self._messages.append(msg)
    log.info("Received MESSAGE from %s: %s", prm.fromUri, prm.msgBody[:50])

def get_messages(self, last_n=None):
    msgs = list(self._messages)
    if last_n:
        msgs = msgs[-last_n:]
    return msgs
```

In `src/account_manager.py`, add to `AccountManager`:

```python
def send_message(self, dest_uri: str, body: str, content_type: str = "text/plain") -> None:
    """Send SIP MESSAGE via a temporary Buddy object.

    Account.sendInstantMessage() doesn't exist in PJSUA2 —
    messaging goes through Buddy.sendInstantMessage().
    """
    if not self._account or not self._account.isValid():
        raise RuntimeError("No valid account — register first")
    buddy_cfg = pj.BuddyConfig()
    buddy_cfg.uri = dest_uri
    buddy = pj.Buddy()
    buddy.create(self._account, buddy_cfg)
    prm = pj.SendInstantMessageParam()
    prm.content = body
    prm.contentType = content_type
    buddy.sendInstantMessage(prm)
```

Note: Asterisk must have `res_pjsip_messaging` loaded to relay SIP MESSAGE.
Verify `tests/asterisk/modules.conf` does NOT noload it (autoload=yes handles this).

In `src/server.py`:

```python
@mcp.tool()
async def send_message(dest_uri: str, body: str) -> dict[str, Any]:
    """Send a SIP MESSAGE (instant text message).

    Args:
        dest_uri: Destination SIP URI (e.g. "sip:6002@asterisk")
        body: Message text content
    """
    ...

@mcp.tool()
async def get_messages(last_n: int | None = None) -> dict[str, Any]:
    """Get received SIP messages.

    Args:
        last_n: Return only last N messages (default: all)
    """
    ...
```

- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Run full test suite**
- [ ] **Step 6: Commit**

```bash
git add src/account_manager.py src/server.py tests/
git commit -m "feat: add SIP MESSAGE send/receive tools"
```

---

## Task 6: Conference (3-way Calling)

Connect multiple calls together through the PJSUA2 conference bridge. The bridge already exists — we just need to connect call audio ports to each other.

### Files:
- Modify: `src/call_manager.py` — add `conference()` method
- Modify: `src/server.py` — add `conference` MCP tool
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
@skip_no_domain
class TestConference:
    @pytest.fixture(autouse=True)
    def mcp_trio(self):
        with McpClient() as a, McpClient() as b, McpClient() as c:
            a.send_initialize()
            b.send_initialize()
            c.send_initialize()
            self.ua_a = a
            self.ua_b = b
            self.ua_c = c
            yield

    def test_three_way_conference(self):
        """A calls B, A calls C, then A bridges all three into a conference."""
        _configure_and_register(self.ua_a, SIP_USER_A, SIP_PASS_A)
        # B and C with auto_answer
        for ua, user, pwd in [(self.ua_b, SIP_USER_B, SIP_PASS_B),
                               (self.ua_c, SIP_USER_C, SIP_PASS_C)]:
            _parse_tool_result(ua.call_tool("configure", {
                "domain": SIP_DOMAIN, "transport": "udp",
                "username": user, "password": pwd, "auto_answer": True,
            }))
            _parse_tool_result(ua.call_tool("register"))
            _wait_registered(ua)

        # A calls B
        r1 = _parse_tool_result(self.ua_a.call_tool("make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        time.sleep(2)

        # A calls C
        r2 = _parse_tool_result(self.ua_a.call_tool("make_call", {
            "dest_uri": f"sip:{SIP_USER_C}@{SIP_DOMAIN}",
        }))
        time.sleep(2)

        # A bridges both calls
        result = _parse_tool_result(self.ua_a.call_tool("conference", {
            "call_ids": [r1["call_id"], r2["call_id"]],
        }))
        assert result["status"] == "ok"

        time.sleep(1)
        # All calls should still be CONFIRMED
        for cid in [r1["call_id"], r2["call_id"]]:
            info = _parse_tool_result(
                self.ua_a.call_tool("get_call_info", {"call_id": cid})
            )
            assert info["state"] == "CONFIRMED"

        # Cleanup
        self.ua_a.call_tool("hangup", {"call_id": r1["call_id"]})
        self.ua_a.call_tool("hangup", {"call_id": r2["call_id"]})
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement conference**

In `src/call_manager.py`, first add a public property to `SipCall`:

```python
@property
def audio_media(self) -> pj.AudioMedia | None:
    return self._aud_med
```

Then add to `CallManager`:

```python
def conference(self, call_ids: list[int]) -> dict[str, Any]:
    """Bridge multiple calls together via the conference bridge.

    Connects audio media of all specified calls to each other so all
    parties can hear each other.
    """
    calls = [self._get_call_by_id(cid) for cid in call_ids]
    media_ports = []
    for call in calls:
        if call.audio_media is not None:
            media_ports.append(call.audio_media)

    if len(media_ports) < 2:
        raise RuntimeError("Need at least 2 calls with active audio for conference")

    # Cross-connect all audio ports
    for i, port_a in enumerate(media_ports):
        for j, port_b in enumerate(media_ports):
            if i != j:
                try:
                    port_a.startTransmit(port_b)
                except Exception:
                    log.debug("Already connected or error: %d→%d", i, j)

    return {"call_ids": call_ids, "participants": len(media_ports)}
```

- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Run full test suite**
- [ ] **Step 6: Commit**

```bash
git add src/call_manager.py src/server.py tests/test_integration.py
git commit -m "feat: add conference tool for 3-way calling via bridge"
```

---

## Task 7: Call History

Track completed calls with metadata — remote URI, duration, status code, recording path. Stored in memory (deque), queryable via MCP tool.

### Files:
- Modify: `src/call_manager.py` — add `_call_history` deque, populate on DISCONNECTED
- Modify: `src/server.py` — add `get_call_history` MCP tool
- Modify: `tests/test_call_manager.py` — unit test for history
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write unit test**

```python
# In tests/test_call_manager.py:

class TestCallHistory:
    def test_empty_history(self, call_mgr):
        assert call_mgr.get_call_history() == []
```

- [ ] **Step 2: Write integration test**

```python
def test_call_history(self):
    """After a call, history contains the call record."""
    self._register_both()

    self.caller.call_tool("make_call", {
        "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
    })
    self._wait_and_answer(self.callee)
    time.sleep(1)
    self.caller.call_tool("hangup")
    time.sleep(1)

    result = _parse_tool_result(self.caller.call_tool("get_call_history"))
    assert result["total_count"] >= 1
    entry = result["history"][0]
    assert "remote_uri" in entry
    assert "duration" in entry
    assert "recording_file" in entry
```

- [ ] **Step 3: Implement call history**

In `src/call_manager.py`, add to `CallManager.__init__`:

```python
self._call_history: deque[dict] = deque(maxlen=100)
```

In `SipCall.onCallState`, when DISCONNECTED, notify CallManager. Add a callback:

```python
# In SipCall.__init__:
self.on_disconnected_cb: Any = None

# In SipCall.onCallState, after _stop_recording/_stop_player:
if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
    self._stop_recording()
    self._stop_player()
    if self.on_disconnected_cb:
        self.on_disconnected_cb(self.get_cached_info())
```

In `CallManager._on_incoming_call` and `make_call`, set the callback:

```python
call.on_disconnected_cb = self._on_call_disconnected
```

```python
def _on_call_disconnected(self, info: dict) -> None:
    self._call_history.append({
        "remote_uri": info.get("remote_uri", ""),
        "duration": info.get("duration", 0),
        "last_status": info.get("last_status", 0),
        "last_status_text": info.get("last_status_text", ""),
        "codec": info.get("codec", ""),
        "recording_file": info.get("recording_file"),
        "timestamp": datetime.now().isoformat(),
    })

def get_call_history(self, last_n: int | None = None) -> list[dict]:
    history = list(self._call_history)
    if last_n:
        history = history[-last_n:]
    return history
```

In `src/server.py`:

```python
@mcp.tool()
async def get_call_history(last_n: int | None = None) -> dict[str, Any]:
    """Get history of completed calls.

    Args:
        last_n: Return only last N calls (default: all)
    """
    assert call_mgr is not None
    try:
        all_history = call_mgr.get_call_history()
        filtered = all_history[-last_n:] if last_n else all_history
        return {"history": filtered, "total_count": len(all_history)}
    except Exception as e:
        log.exception("get_call_history failed")
        return {"status": "error", "error": str(e)}
```

- [ ] **Step 4: Run tests to verify they pass**
- [ ] **Step 5: Run full test suite**
- [ ] **Step 6: Commit**

```bash
git add src/call_manager.py src/server.py tests/
git commit -m "feat: add call history tracking with get_call_history tool"
```

---

## Execution Order

1. **Task 1: Auto-Answer** — prerequisite for Tasks 3, 4, 6 (auto-answer C in transfer/conference tests)
2. **Task 2: Reject Call** — independent, simple
3. **Task 7: Call History** — independent, simple
4. **Task 5: SIP MESSAGE** — independent, different subsystem
5. **Task 3: Blind Transfer** — needs auto-answer + extension 6003
6. **Task 4: Attended Transfer** — needs blind transfer infra
7. **Task 6: Conference** — needs 3 extensions + auto-answer

## Final Verification

After all tasks:

```bash
# Unit tests
docker compose run --rm --entrypoint pytest pjsua-mcp tests/ -m "not integration" -v

# Integration tests
docker compose -f docker-compose.test.yml run --build --rm test-runner

# Expected: 26+ unit tests, 16+ integration tests, all green
```

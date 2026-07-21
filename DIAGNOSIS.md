# 🔍 FULL DIAGNOSIS — "Session's transaction has been rolled back" / exam_id NULL crash

## 1. Jo error dikh raha hai (do roop, ek hi jad)

**Roop A (pehle):**
```
(psycopg2.errors.NotNullViolation) null value in column "exam_id"
of relation "attempts" violates not-null constraint
```

**Roop B (ab):**
```
This Session's transaction has been rolled back due to a previous exception
during flush ... (raised as a result of Query-invoked autoflush)
```

**Ye dono ek hi problem ke do chehre hain.** Roop B, Roop A ka **after-effect** hai.

---

## 2. Asli jad (root cause) — step by step

### Postgres vs SQLite farak
- **SQLite** (local): lenient hai. Ek statement fail ho to bhi session chalti rehti hai. Isliye local pe error **reproduce nahi** hota.
- **Postgres** (Render/live): strict hai (SQL standard). Ek transaction ke andar **ek bhi statement fail** ho jaye, to **poori transaction "aborted"** ho jaati hai. Uske baad us session pe koi bhi query chalao → yehi milta hai:
  *"transaction has been rolled back due to a previous exception during flush"*.

### Chain of events (jo Render pe hua)
1. `POST /api/attempts/start` chala.
2. Beech me kahin ek **INSERT/UPDATE flush** hua jo Postgres pe **fail** hua
   (sabse pehla failure = `attempts.exam_id` NULL, kyunki resume/expire path me
   `db.session.rollback()` ke baad `exam` ORM object **detach/expire** ho gaya
   tha aur `exam.id` NULL de raha tha).
3. Us fail ke baad **session poison** ho gayi (Postgres transaction aborted).
4. Aage jo bhi code `.all()` / `.first()` / `.count()` chalata hai, wo
   **autoflush** trigger karta hai → wahi poisoned-session error (Roop B).
5. Kyunki error kabhi properly `rollback()` nahi hua **usi request ke andar**,
   aur ek stuck/adhoora attempt DB me ban gaya, ye baar-baar dikhta rehta hai.

### Do alag technical bugs jo is chain ko banate hain
- **Bug-1 (source):** resume/auto-expire branch `commit()`/`rollback()` karta hai
  jo `exam` object ko expire/detach kar deta hai. Uske baad `exam.id`, ya us
  detached object se juda koi bhi attribute, Postgres pe NULL/error de sakta hai.
- **Bug-2 (amplifier):** jab pehla flush fail hota hai, code **usi request me**
  session ko clean (rollback) kiye bina aage badhta rehta hai, to har agla query
  autoflush pe crash karti hai. (SQLite pe chhup jaata hai, Postgres pe nahi.)
- **Bug-3 (leftover):** ek purana **toota attempt (exam_id=NULL)** DB me pada hai
  (jab pehli baar crash hua tha). Wo resume path ko phir-phir trigger karke wahi
  error dikhata rehta hai — chahe code fix ho jaye.

---

## 3. Kya hona CHAHIYE (sahi design)

1. Attempt banate waqt `exam_id` **hamesha** ek valid integer ho — kabhi NULL nahi.
   (Request se aaya validated int use karo, ORM attribute pe bharosa mat karo.)
2. Resume/expire path ke commit/rollback ke baad `exam` ko **fresh re-fetch** karo.
3. Koi bhi flush/commit `try/except` me ho, aur **fail hote hi usi jagah
   `db.session.rollback()`** ho — taaki session poison na rahe aur saaf error
   mile (500 + JSON message), autoflush-cascade nahi.
4. Ek `no_autoflush` block use karo un read-heavy jagahon pe jahan hum abhi
   objects bana rahe hain (taaki adhoora object autoflush pe insert na ho jaye).
5. Purane **toote attempts** (exam_id NULL ya orphan) ko DB se **saaf** karne ka
   ek admin action ho — jise ek baar chalane pe stuck-crash khatm ho jaye.
6. **Guarantee:** DB-level pe `attempts.exam_id` NULL insert ka koi raasta na bache.

---

## 4. Fix plan (jo main laga raha hu)

| # | Fix | File |
|---|-----|------|
| 1 | `start_attempt` ko ek clean, no_autoflush, fail-safe block me rebuild | attempts.py |
| 2 | Har attempt-write flush/commit ke saath turant rollback-on-error | attempts.py |
| 3 | `exam` ko validated id se re-fetch (pehle se hai, pukka karenge) | attempts.py |
| 4 | Cleanup endpoint: toote attempts + purane top-level tests + containers hatao | exams.py |
| 5 | Ek startup-safe guard: exam_id NULL kabhi insert na ho | attempts.py |
| 6 | Test: Postgres-jaisa strict scenario reproduce karke pass karao | (test) |

---

## 5. Verify kaise karenge
- Local pe strict-mode simulate: poisoned session ke baad bhi clean 500 aaye,
  cascade nahi.
- Real test-client se: first start (201), second start/resume (200),
  expired-attempt start (auto-expire + naya 201) — sab clean.
- Cleanup ke baad koi exam_id-NULL attempt na bache.

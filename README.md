# أرشيفي — حفظ تلقائي لمنشورات تيك توك وأسئلة تيلونيم

**كيف يعمل:**
- `GitHub Actions` يشغّل `worker/saver.py` كل 3 ساعات، فيحفظ الجديد فقط في **Supabase** (الفيديوهات في Storage، والبيانات في قاعدة البيانات).
- `index.html` (على GitHub Pages) لوحة تصفح خاصة، تحتاج تسجيل دخول.
- الحسابات تُعدَّل من `config.json`.

## خطوات التشغيل (مرة واحدة)

1. **Supabase:** أنشئ مشروعاً جديداً ← SQL Editor ← الصق محتوى `supabase/schema.sql` ← Run.
2. **حسابك للدخول:** Authentication ← Users ← Add user (بريد وكلمة مرور).
   ثم Authentication ← Sign In / Providers ← **عطّل "Allow new users to sign up"** (مهم حتى لا يسجّل غيرك ويشاهد الأرشيف).
3. **المفاتيح:** Project Settings ← API، وانسخ:
   - `Project URL`
   - `anon public key` ← يوضع في `index.html` (آمن لأن الأرشيف محمي بتسجيل الدخول).
   - `service_role key` ← **سري جداً**، لا يوضع في أي ملف، فقط في Secrets.
4. **GitHub ← Settings ← Secrets and variables ← Actions ← New repository secret:**
   - `SUPABASE_URL` = رابط المشروع
   - `SUPABASE_SERVICE_KEY` = مفتاح service_role
   - `TIKTOK_COOKIES` (اختياري) = محتوى ملف cookies.txt إذا حجب تيك توك الطلبات
5. عدّل أول سطرين في `index.html` (`SUPABASE_URL` و `SUPABASE_ANON_KEY`).
6. **GitHub ← Settings ← Pages ←** Deploy from a branch ← `main` / root.
7. **تبويب Actions ← Save posts ← Run workflow** لأول تشغيل. بعدها يعمل تلقائياً.

## ملاحظات مهمة
- كل تشغيل يحفظ حتى 20 فيديو جديداً (`max_new_videos_per_run` في `config.json`)، فالأرشيف القديم الكبير يكتمل على عدة تشغيلات.
- الخطة المجانية في Supabase تعطي حوالي 1GB تخزين؛ الفيديوهات قد تملؤها. راقب المساحة من Dashboard ← Storage.
- تيك توك وتيلونيم قد يحجبان عناوين GitHub Actions. إذا فشل التشغيل ستجد السبب في سجل الـ Action.
- تيلونيم يستخدم واجهة غير رسمية وقد تتغير.
- GitHub يوقف التشغيل المجدول بعد 60 يوماً بلا نشاط في المستودع، فشغّله يدوياً مرة لإعادة تفعيله.
- استخدمه لحساباتك أو حسابات تملك إذنها.

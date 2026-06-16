/* ============================================================
Pagos Recibidos · lógica de la interfaz
Soporta múltiples empresas (TAJIBOS / Burger King)
============================================================ */

const $ = (sel) => document.querySelector(sel);

const dropzone   = $("#dropzone");
const fileInput  = $("#fileInput");
const overlay    = $("#overlay");
const overlayTxt = $("#overlayText");
const toast      = $("#toast");

const panelUpload  = $("#panel-upload");
const panelPreview = $("#panel-preview");
const panelResult  = $("#panel-result");

const fmtMoney = (n) =>
new Intl.NumberFormat("es-BO", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);

// ── Helpers de UI ────────────────────────────────────────────
function showToast(msg, isError = true) {
toast.textContent = msg;
toast.classList.toggle("is-error", isError);
toast.classList.remove("is-hidden");
setTimeout(() => toast.classList.add("is-hidden"), 4500);
}

function showOverlay(text) { overlayTxt.textContent = text; overlay.classList.remove("is-hidden"); }
function hideOverlay()      { overlay.classList.add("is-hidden"); }

function setStep(n) {
document.querySelectorAll(".step").forEach((s) => {
    const step = Number(s.dataset.step);
    s.classList.toggle("is-active", step === n);
    s.classList.toggle("is-done",   step < n);
});
}

function showPanel(panel) {
[panelUpload, panelPreview, panelResult].forEach((p) => p.classList.add("is-hidden"));
panel.classList.remove("is-hidden");
}

function getEmpresaId() {
const checked = document.querySelector('input[name="empresa"]:checked');
return checked ? checked.value : "lth";
}

// ── Toggle SAP real ──────────────────────────────────────────
const sapToggle = $("#sapRealToggle");
const envBadge  = $("#envBadge");
const envLabel  = $("#envLabel");

sapToggle?.addEventListener("change", () => {
const real = sapToggle.checked;
envBadge.classList.toggle("is-real", real);
envLabel.textContent = real ? "SAP real" : "Simulación";
});

// ── Drag & drop / click ──────────────────────────────────────
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => {
if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});

["dragover", "dragenter"].forEach((ev) =>
dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("is-drag"); })
);
["dragleave", "drop"].forEach((ev) =>
dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("is-drag"); })
);
dropzone.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); });
fileInput.addEventListener("change", () => { if (fileInput.files[0]) uploadFile(fileInput.files[0]); });

// ── Previsualización ─────────────────────────────────────────
async function uploadFile(file) {
if (!file.name.toLowerCase().endsWith(".xlsx")) {
    showToast("El archivo debe ser .xlsx");
    return;
}

const empresaId = getEmpresaId();
showOverlay("Leyendo el archivo…");

const fd = new FormData();
fd.append("file", file);
fd.append("empresa", empresaId);

try {
    const res  = await fetch("/api/preview", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Error al leer el archivo");
    renderPreview(data);
    setStep(2);
    showPanel(panelPreview);
} catch (err) {
    showToast(err.message);
} finally {
    hideOverlay();
    fileInput.value = "";
}
}

function renderPreview(data) {
$("#previewFileName").textContent = data.file_name;
$("#previewMeta").textContent =
    `${data.total} fila${data.total !== 1 ? "s" : ""} detectada${data.total !== 1 ? "s" : ""}`;

$("#previewEmpresa").textContent = `↳ ${data.empresa}`;

$("#previewSummary").innerHTML = `
    <div class="chip ok"><div class="chip__val">${data.validas}</div><div class="chip__lbl">Válidas</div></div>
    <div class="chip ${data.invalidas ? "err" : ""}">
    <div class="chip__val">${data.invalidas}</div><div class="chip__lbl">Con problemas</div>
    </div>
    <div class="chip">
    <div class="chip__val">${fmtMoney(data.total_monto)}</div>
    <div class="chip__lbl">Monto total (Bs)</div>
    </div>
`;

const tbody = $("#previewTable tbody");
tbody.innerHTML = data.rows.map((r) => `
    <tr>
    <td class="num">${r.linea}</td>
    <td>${r.fecha || "—"}</td>
    <td>${r.tipo || "—"}</td>
    <td class="num">${r.valido ? fmtMoney(r.monto) : "—"}</td>
    <td class="mono">${r.cuenta_banco || "—"}</td>
    <td class="mono">${r.cuenta_destino || "—"}</td>
    <td class="mono">${r.centro_costo || "—"}</td>
    <td>${r.descripcion || "—"}</td>
    <td>${r.valido
        ? '<span class="badge valido">OK</span>'
        : `<span class="badge invalido" title="${r.error}">Revisar</span>`
    }</td>
    </tr>
`).join("");
}

// ── Procesar ─────────────────────────────────────────────────
$("#btnProcess").addEventListener("click", async () => {
const real = sapToggle.checked;
if (real && !confirm("Vas a insertar los pagos en SAP REAL. ¿Confirmás?")) return;

showOverlay(real ? "Insertando pagos en SAP…" : "Simulando inserción…");

try {
    const res  = await fetch("/api/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sap_real: real }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Error al procesar");
    renderResult(data);
    setStep(3);
    showPanel(panelResult);
} catch (err) {
    showToast(err.message);
} finally {
    hideOverlay();
}
});

function renderResult(data) {
$("#resultTitle").textContent = data.resultado === "OK"
    ? "Procesado correctamente"
    : "Procesado con incidencias";
$("#resultMode").textContent = `Modo: ${data.modo}`;
$("#resultEmpresa").textContent = `↳ ${data.empresa}`;

$("#resultSummary").innerHTML = `
    <div class="chip ok">
    <div class="chip__val">${data.exitos}</div><div class="chip__lbl">Éxito</div>
    </div>
    <div class="chip ${data.errores ? "err" : ""}">
    <div class="chip__val">${data.errores}</div><div class="chip__lbl">Error</div>
    </div>
    <div class="chip ${data.observados ? "warn" : ""}">
    <div class="chip__val">${data.observados}</div><div class="chip__lbl">Observado</div>
    </div>
    <div class="chip ${data.omitidas ? "skip" : ""}">
    <div class="chip__val">${data.omitidas}</div><div class="chip__lbl">Omitido</div>
    </div>
`;

const tbody = $("#resultTable tbody");
tbody.innerHTML = data.resultados.map((r) => `
    <tr>
    <td class="num">${r.linea}</td>
    <td><span class="badge ${r.estado}">${r.estado}</span></td>
    <td class="num">${r.doc_entry ?? "—"}</td>
    <td class="num">${r.doc_num  ?? "—"}</td>
    <td class="mono">${r.cuenta  || "—"}</td>
    <td>${r.error || "—"}</td>
    </tr>
`).join("");
}

// ── Reiniciar / descargar ────────────────────────────────────
function reset() { setStep(1); showPanel(panelUpload); }
$("#btnReset").addEventListener("click", reset);
$("#btnNew").addEventListener("click", reset);
$("#btnDownload").addEventListener("click", () => { window.location.href = "/api/report"; });
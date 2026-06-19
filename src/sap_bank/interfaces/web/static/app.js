const $ = (sel) => document.querySelector(sel);

const dropzone   = $("#dropzone");
const fileInput  = $("#fileInput");
const overlay    = $("#overlay");
const overlayTxt = $("#overlayText");
const toast      = $("#toast");
const modal      = $("#modal");

const panelUpload  = $("#panel-upload");
const panelPreview = $("#panel-preview");
const panelResult  = $("#panel-result");

const fmtMoney = (n) =>
new Intl.NumberFormat("es-BO", {
    minimumFractionDigits: 2, maximumFractionDigits: 2
}).format(n);

function showToast(msg, isError = true) {
toast.textContent = msg;
toast.classList.toggle("is-error", isError);
toast.classList.remove("is-hidden");
setTimeout(() => toast.classList.add("is-hidden"), 4500);
}
function showOverlay(text) { overlayTxt.textContent = text; overlay.classList.remove("is-hidden"); }
function hideOverlay()      { overlay.classList.add("is-hidden"); }
function showModal(body)    { $("#modalBody").textContent = body; modal.classList.remove("is-hidden"); }
function hideModal()        { modal.classList.add("is-hidden"); }

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
return checked ? parseInt(checked.value) : null;
}

// ── Modal ─────────────────────────────────────────────────────
$("#modalCancel").addEventListener("click", hideModal);
$("#modalConfirm").addEventListener("click", () => { hideModal(); ejecutarInsercion(); });

// ── Drag & drop ───────────────────────────────────────────────
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => {
if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});
["dragover","dragenter"].forEach(ev =>
dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("is-drag"); })
);
["dragleave","drop"].forEach(ev =>
dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("is-drag"); })
);
dropzone.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); });
fileInput.addEventListener("change", () => { if (fileInput.files[0]) uploadFile(fileInput.files[0]); });

// ── Preview ───────────────────────────────────────────────────
async function uploadFile(file) {
if (!file.name.toLowerCase().endsWith(".xlsx")) {
    showToast("El archivo debe ser .xlsx"); return;
}
const empresaId = getEmpresaId();
if (!empresaId) { showToast("Seleccioná una empresa primero."); return; }

showOverlay("Leyendo el archivo…");
const fd = new FormData();
fd.append("file", file);
fd.append("empresa_id", empresaId);

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
    hideOverlay(); fileInput.value = "";
}
}

function renderPreview(data) {
$("#previewFileName").textContent = data.file_name;
$("#previewMeta").textContent = `${data.total} fila${data.total !== 1 ? "s" : ""} detectadas`;
$("#previewEmpresa").textContent = data.empresa;

$("#previewSummary").innerHTML = `
    <div class="chip ok"><div class="chip__val">${data.validas}</div><div class="chip__lbl">Válidas</div></div>
    <div class="chip ${data.invalidas ? "err" : ""}">
    <div class="chip__val">${data.invalidas}</div><div class="chip__lbl">Con problemas</div>
    </div>
    <div class="chip">
    <div class="chip__val">${fmtMoney(data.total_monto)}</div>
    <div class="chip__lbl">Monto total Bs</div>
    </div>`;

const note = $("#actionNote");
if (note) {
    note.textContent = data.invalidas > 0
    ? `${data.validas} fila${data.validas !== 1 ? "s" : ""} se insertarán. ${data.invalidas} con problemas se omitirán.`
    : `${data.validas} fila${data.validas !== 1 ? "s" : ""} listas para insertar en SAP.`;
}

$("#previewTable tbody").innerHTML = data.rows.map((r) => `
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
        : `<span class="badge invalido" title="${r.error}">Revisar</span>`}</td>
    </tr>`).join("");
}

// ── Procesar ──────────────────────────────────────────────────
$("#btnProcess").addEventListener("click", () => {
const note = $("#actionNote")?.textContent || "";
showModal(`${note}\n\nEsta acción no se puede deshacer.`);
});

async function ejecutarInsercion() {
showOverlay("Insertando pagos en SAP…");
try {
    const res  = await fetch("/api/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sap_real: true }),
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
}

function renderResult(data) {
$("#resultTitle").textContent = data.resultado === "OK"
    ? "Procesado correctamente" : "Procesado con incidencias";
$("#resultMode").textContent  = `${data.empresa} · ${data.total} filas procesadas`;
$("#resultEmpresa").textContent = data.empresa;

$("#resultSummary").innerHTML = `
    <div class="chip ok"><div class="chip__val">${data.exitos}</div><div class="chip__lbl">Éxito</div></div>
    <div class="chip ${data.errores ? "err" : ""}"><div class="chip__val">${data.errores}</div><div class="chip__lbl">Error</div></div>
    <div class="chip ${data.observados ? "warn" : ""}"><div class="chip__val">${data.observados}</div><div class="chip__lbl">Observado</div></div>
    <div class="chip ${data.omitidas ? "skip" : ""}"><div class="chip__val">${data.omitidas}</div><div class="chip__lbl">Omitido</div></div>`;

$("#resultTable tbody").innerHTML = data.resultados.map((r) => `
    <tr>
    <td class="num">${r.linea}</td>
    <td><span class="badge ${r.estado}">${r.estado}</span></td>
    <td class="num mono">${r.doc_entry ?? "—"}</td>
    <td class="num mono">${r.doc_num  ?? "—"}</td>
    <td class="mono">${r.cuenta  || "—"}</td>
    <td>${r.error || "—"}</td>
    </tr>`).join("");
}

// ── Reset / Descarga ──────────────────────────────────────────
function reset() { setStep(1); showPanel(panelUpload); }
$("#btnReset").addEventListener("click", reset);
$("#btnNew").addEventListener("click", reset);
$("#btnDownload").addEventListener("click", () => { window.location.href = "/api/report"; });
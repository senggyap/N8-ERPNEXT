// NKT ERP — QuickBooks Desktop 2009-inspired visual foundation.
// Presentation tagging only. No values, permissions, routes, transactions, or buttons are changed.
(() => {
  const CLASS_NAME = "nkt-qb2009-shell";

  function routeTag() {
    try {
      const route = (window.frappe && frappe.get_route) ? frappe.get_route() : [];
      return (route || []).map((part) => String(part).toLowerCase().replace(/[^a-z0-9]+/g, "-"))
        .filter(Boolean).slice(0, 3).join("--") || "desk";
    } catch (e) {
      return "desk";
    }
  }

  function applyVisualShell() {
    document.documentElement.classList.add(CLASS_NAME);
    if (document.body) {
      document.body.classList.add(CLASS_NAME);
      document.body.setAttribute("data-nkt-visual-reference", "quickbooks-desktop-2009-inspired");
      document.body.setAttribute("data-nkt-route", routeTag());
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", applyVisualShell, { once: true });
  } else {
    applyVisualShell();
  }

  if (window.frappe && frappe.router && frappe.router.on) {
    frappe.router.on("change", applyVisualShell);
  }
})();


// NKT POST-C15F VP2B2 — presentation-only route family tagging.
(() => {
 const families=["cashier","encoder","history","shift","return","supplier","warehouse","trucking","oil","admin"];
 function classify(){
  if(!document.body)return;
  families.forEach(f=>document.body.classList.remove("nkt-family-"+f));
  let route="";try{route=((window.frappe&&frappe.get_route)?frappe.get_route():[]).join(" ").toLowerCase()}catch(e){}
  let f="admin";
  if(route.includes("cashier fast")||route.includes("cashier sale"))f="cashier";
  else if(route.includes("encoder")||route.includes("customer order"))f="encoder";
  else if(route.includes("customer 360")||route.includes("item history")||route.includes("customer history"))f="history";
  else if(route.includes("cashier shift")||route.includes("z-out")||route.includes("z out"))f="shift";
  else if(route.includes("return")||route.includes("exchange"))f="return";
  else if(route.includes("supplier receiving"))f="supplier";
  else if(route.includes("warehouse release")||route.includes("physical inventory")||route.includes("warehouse transfer"))f="warehouse";
  else if(route.includes("trucking")||route.includes("driver incentive")||route.includes("carrier payable"))f="trucking";
  else if(route.includes("oil")||route.includes("repack"))f="oil";
  document.body.classList.add("nkt-family-"+f);document.body.setAttribute("data-nkt-screen-family",f);
 }
 if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",classify,{once:true});else classify();
 if(window.frappe&&frappe.router&&frappe.router.on)frappe.router.on("change",classify);
})();


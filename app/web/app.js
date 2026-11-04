"use strict";

// The token can open the user's private chats, so it stays in memory only:
// reloading the page signs out. All page text is set through textContent.
const state = {
  token: null, username: "", roles: [], view: "chat", thread: null, threads: [], counts: {}, poll: null,
  bellPoll: null, unread: 0,
};
const root = document.getElementById("app");

const OUTCOMES = {
  ok: ["Completed", "good"],
  forwarded: ["Sent to accounts", "warn"],
  pending_approval: ["Waiting for approval", "warn"],
  flagged_nsfw: ["Blocked and reported", "bad"],
  denied: ["Denied", "bad"],
  denied_auth: ["Not allowed", "bad"],
  denied_quota: ["Rate limited", "bad"],
  denied_busy: ["Busy", "warn"],
  error: ["Failed", "bad"],
};

const REQUEST_LABELS = {
  manage_bill: "Record a bill",
  initiate_payment: "Make a payment",
  manage_order: "Change an order",
  book_service: "Book a service",
  order: "Order",
  service: "Service booking",
  bill: "Bill",
};

const REQUEST_STATES = {
  pending: ["Waiting for approval", "warn"],
  approved: ["Approved", "good"],
  denied: ["Denied", "bad"],
  failed: ["Failed", "bad"],
  queued: ["With the accounts team", "warn"],
  processing: ["In progress", "warn"],
  awaiting: ["Waiting for approval", "warn"],
  done: ["Done", "good"],
};
const FINISHED = new Set(["approved", "denied", "failed", "done"]);

const DEPARTMENTS = ["account", "hr", "logistics", "researcher", "superuser"];
const DEPARTMENT_NAMES = {
  account: "Accounts", hr: "HR", logistics: "Logistics", researcher: "Research", superuser: "Superuser", admin: "Admin",
};
const departmentName = (name) => DEPARTMENT_NAMES[name] || name;

// Suggestions under the empty chat: [text, icon]. Department ones first, then personal ones.
const IDEAS = {
  account: ["Show the ledger summary", "receipt_long"],
  logistics: ["Track shipment SHP-8842", "local_shipping"],
  researcher: ["Summarize the Q3 research notes", "science"],
  hr: ["Any new candidate emails in the careers inbox?", "mail"],
  superuser: ["Check stock of laptops", "inventory_2"],
};
const PERSONAL_IDEAS = [
  ["What's new in my mail?", "mail"],
  ["Analyse a CSV and show me a dashboard", "monitoring"],
  ["Remind me tomorrow at 9 to call the bank", "alarm"],
  ["Help me write a polite email", "edit_square"],
];

const ICONS = {
  plus: "M12 5v14M5 12h14",
  send: "M12 19V5M6 11l6-6 6 6",
  menu: "M4 7h16M4 12h16M4 17h16",
  pen: "M4 20l1-4L16 5l3 3L8 19l-4 1z",
  spark: "M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z",
  bell: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.5 2.5 3.8 5.5 3.8 9s-1.3 6.5-3.8 9c-2.5-2.5-3.8-5.5-3.8-9S9.5 5.5 12 3z",
};

// Material Symbols (Outlined, weight 400; Apache License 2.0), drawn by mi(). viewBox 0 -960 960 960.
const MI = {
  account_circle: "M222-255q63-44 125-67.5T480-346q71 0 133.5 23.5T739-255q44-54 62.5-109T820-480q0-145-97.5-242.5T480-820q-145 0-242.5 97.5T140-480q0 61 19 116t63 109Zm160.5-234.5Q343-529 343-587t39.5-97.5Q422-724 480-724t97.5 39.5Q617-645 617-587t-39.5 97.5Q538-450 480-450t-97.5-39.5ZM480-80q-82 0-155-31.5t-127.5-86Q143-252 111.5-325T80-480q0-83 31.5-155.5t86-127Q252-817 325-848.5T480-880q83 0 155.5 31.5t127 86q54.5 54.5 86 127T880-480q0 82-31.5 155t-86 127.5q-54.5 54.5-127 86T480-80Zm107.5-76Q640-172 691-212q-51-36-104-55t-107-19q-54 0-107 19t-104 55q51 40 103.5 56T480-140q55 0 107.5-16Zm-52-375.5Q557-553 557-587t-21.5-55.5Q514-664 480-664t-55.5 21.5Q403-621 403-587t21.5 55.5Q446-510 480-510t55.5-21.5ZM480-587Zm0 374Z",
  add: "M450-450H200v-60h250v-250h60v250h250v60H510v250h-60v-250Z",
  alarm: "M339.5-110q-65.5-28-114-76.5t-77-114Q120-366 120-440.73q0-74.74 28.5-140Q177-646 225.5-695t114-77Q405-800 479-800t139.5 28Q684-744 733-695t77 114.27q28 65.26 28 140 0 74.73-28 140.23-28 65.5-77 114T618.5-110Q553-82 479-82t-139.5-28ZM479-439Zm121 161 42-42-130-130v-190h-60v214l148 148ZM214-867l42 42L92-667l-42-42 164-158Zm530 0 164 158-42 42-164-158 42-42ZM479.04-142Q604-142 691-229.04q87-87.05 87-212Q778-566 690.96-653q-87.05-87-212-87Q354-740 267-652.96q-87 87.05-87 212Q180-316 267.04-229q87.05 87 212 87Z",
  alternate_email: "M480-80q-83 0-156-31.5T197-197q-54-54-85.5-127T80-480q0-83 31.5-156T197-763q54-54 127-85.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480v53q0 56-39.34 94.5Q801.31-294 744-294q-36.08 0-68.04-17.5Q644-329 627-361q-26 34-65.08 50.5Q522.83-294 480-294q-77.61 0-132.3-54Q293-402 293-480.01q0-78.02 54.5-133Q402-668 480-668t132.5 54.99Q667-558.02 667-480v53q0 30.61 22.5 51.81Q712-354 743.5-354t54-21.19Q820-396.39 820-427v-53q0-142.38-99-241.19T480-820q-142 0-241 99t-99 241q0 142 98.81 241 98.82 99 241.19 99h214v60H480Zm90-310.75q37-36.75 37-89.25 0-54-37.06-91t-90-37Q427-608 390-571t-37 91q0 52.5 37.06 89.25t90 36.75Q533-354 570-390.75Z",
  arrow_forward: "M686-450H160v-60h526L438-758l42-42 320 320-320 320-42-42 248-248Z",
  arrow_upward: "M450-160v-526L202-438l-42-42 320-320 320 320-42 42-248-248v526h-60Z",
  attach_file: "M728-326q0 103-72.18 174.5-72.17 71.5-175 71.5Q378-80 305.5-151.5T233-326v-380q0-72.5 51.5-123.25T408-880q72 0 123.5 50.75T583-706v360q0 42-30 72t-72.5 30q-42.5 0-72.5-29.67-30-29.68-30-72.33v-370h60v370q0 17 12.5 29.5t30.64 12.5q18.14 0 30-12.5T523-346v-360q0-48-33.5-81t-81.71-33q-48.21 0-81.5 33.06T293-706v380q0 78 54.97 132T481-140q77.92 0 132.46-54Q668-248 668-326v-390h60v390Z",
  bar_chart: "M660-160v-280h140v280H660Zm-250 0v-640h140v640H410Zm-250 0v-440h140v440H160Z",
  block: "M324-111.5Q251-143 197-197t-85.5-127Q80-397 80-480t31.5-156Q143-709 197-763t127-85.5Q397-880 480-880t156 31.5Q709-817 763-763t85.5 127Q880-563 880-480t-31.5 156Q817-251 763-197t-127 85.5Q563-80 480-80t-156-31.5ZM480-140q61.01 0 117.51-20.5Q654-181 699-220L220-699q-38 46-59 102.17T140-480q0 142.37 98.81 241.19Q337.63-140 480-140Zm259-121q37-45 59-101.49 22-56.5 22-117.51 0-142.38-98.81-241.19T480-820q-60.66 0-116.83 21T261-739l478 478ZM480-480Z",
  chat_bubble: "M80-80v-740q0-24 18-42t42-18h680q24 0 42 18t18 42v520q0 24-18 42t-42 18H240L80-80Zm134-220h606v-520H140v600l74-80Zm-74 0v-520 520Z",
  check_circle: "m421-298 283-283-46-45-237 237-120-120-45 45 165 166Zm59 218q-82 0-155-31.5t-127.5-86Q143-252 111.5-325T80-480q0-83 31.5-156t86-127Q252-817 325-848.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 82-31.5 155T763-197.5q-54 54.5-127 86T480-80Zm0-60q142 0 241-99.5T820-480q0-142-99-241t-241-99q-141 0-240.5 99T140-480q0 141 99.5 240.5T480-140Zm0-340Z",
  chevron_right: "M530-481 332-679l43-43 241 241-241 241-43-43 198-198Z",
  close: "m249-207-42-42 231-231-231-231 42-42 231 231 231-231 42 42-231 231 231 231-42 42-231-231-231 231Z",
  dark_mode: "M480-120q-150 0-255-105T120-480q0-150 105-255t255-105q8 0 17 .5t23 1.5q-36 32-56 79t-20 99q0 90 63 153t153 63q52 0 99-18.5t79-51.5q1 12 1.5 19.5t.5 14.5q0 150-105 255T480-120Zm0-60q109 0 190-67.5T771-406q-25 11-53.67 16.5Q688.67-384 660-384q-114.69 0-195.34-80.66Q384-545.31 384-660q0-24 5-51.5t18-62.5q-98 27-162.5 109.5T180-480q0 125 87.5 212.5T480-180Zm-4-297Z",
  dashboard: "M510-570v-270h330v270H510ZM120-450v-390h330v390H120Zm390 330v-390h330v390H510Zm-390 0v-270h330v270H120Zm60-390h210v-270H180v270Zm390 330h210v-270H570v270Zm0-450h210v-150H570v150ZM180-180h210v-150H180v150Zm210-330Zm180-120Zm0 180ZM390-330Z",
  delete: "M261-120q-24.75 0-42.37-17.63Q201-155.25 201-180v-570h-41v-60h188v-30h264v30h188v60h-41v570q0 24-18 42t-42 18H261Zm438-630H261v570h438v-570ZM367-266h60v-399h-60v399Zm166 0h60v-399h-60v399ZM261-750v570-570Z",
  description: "M319-250h322v-60H319v60Zm0-170h322v-60H319v60ZM220-80q-24 0-42-18t-18-42v-680q0-24 18-42t42-18h361l219 219v521q0 24-18 42t-42 18H220Zm331-554v-186H220v680h520v-494H551ZM220-820v186-186 680-680Z",
  download: "M480-313 287-506l43-43 120 120v-371h60v371l120-120 43 43-193 193ZM220-160q-24 0-42-18t-18-42v-143h60v143h520v-143h60v143q0 24-18 42t-42 18H220Z",
  edit_square: "M180-120q-24 0-42-18t-18-42v-600q0-24 18-42t42-18h405l-60 60H180v600h600v-348l60-60v408q0 24-18 42t-42 18H180Zm300-360ZM360-360v-170l382-382q9-9 20-13t22-4q11 0 22.32 4.5Q817.63-920 827-911l83 84q8.61 8.96 13.3 19.78 4.7 10.83 4.7 22.02 0 11.2-4.5 22.7T910-742L530-360H360Zm508-425-84-84 84 84ZM420-420h85l253-253-43-42-43-42-252 251v86Zm295-295-43-42 43 42 43 42-43-42Z",
  fact_check: "M132-120q-24 0-42-18t-18-42v-600q0-24 18-42t42-18h696q24 0 42 18t18 42v600q0 24-18 42t-42 18H132Zm0-60h696v-600H132v600Zm68-100h200v-80H200v80Zm382-80 198-198-57-57-141 142-57-57-56 57 113 113Zm-382-80h200v-80H200v80Zm0-160h200v-80H200v80Zm-68 420v-600 600Z",
  flag: "M200-120v-680h343l19 86h238v370H544l-18.93-85H260v309h-60Zm300-452Zm95 168h145v-250H511l-19-86H260v251h316l19 85Z",
  folder: "M140-160q-24 0-42-18.5T80-220v-520q0-23 18-41.5t42-18.5h281l60 60h339q23 0 41.5 18.5T880-680v460q0 23-18.5 41.5T820-160H140Zm0-60h680v-460H456l-60-60H140v520Zm0 0v-520 520Z",
  forum: "M880-81 721-240H300q-24.75 0-42.37-17.63Q240-275.25 240-300v-80h440q24.75 0 42.38-17.63Q740-415.25 740-440v-280h80q24.75 0 42.38 17.62Q880-684.75 880-660v579ZM140-425l75-75h405v-320H140v395ZM80-280v-540q0-24.75 17.63-42.38Q115.25-880 140-880h480q24.75 0 42.38 17.62Q680-844.75 680-820v320q0 24.75-17.62 42.37Q644.75-440 620-440H240L80-280Zm60-220v-320 320Z",
  gavel: "M160-120v-60h480v60H160Zm222-212L160-554l70-72 224 222-72 72Zm254-254L414-810l72-70 222 222-72 72Zm202 426L302-696l42-42 536 536-42 42Z",
  group: "M38-160v-94q0-35 18-63.5t50-42.5q73-32 131.5-46T358-420q62 0 120 14t131 46q32 14 50.5 42.5T678-254v94H38Zm700 0v-94q0-63-32-103.5T622-423q69 8 130 23.5t99 35.5q33 19 52 47t19 63v94H738ZM250-523q-42-42-42-108t42-108q42-42 108-42t108 42q42 42 42 108t-42 108q-42 42-108 42t-108-42Zm426 0q-42 42-108 42-11 0-24.5-1.5T519-488q24-25 36.5-61.5T568-631q0-45-12.5-79.5T519-774q11-3 24.5-5t24.5-2q66 0 108 42t42 108q0 66-42 108ZM98-220h520v-34q0-16-9.5-31T585-306q-72-32-121-43t-106-11q-57 0-106.5 11T130-306q-14 6-23 21t-9 31v34Zm324.5-346.5Q448-592 448-631t-25.5-64.5Q397-721 358-721t-64.5 25.5Q268-670 268-631t25.5 64.5Q319-541 358-541t64.5-25.5ZM358-220Zm0-411Z",
  history: "M477-120q-149 0-253-105.5T120-481h60q0 125 86 213t211 88q127 0 215-89t88-216q0-124-89-209.5T477-780q-68 0-127.5 31T246-667h105v60H142v-208h60v106q52-61 123.5-96T477-840q75 0 141 28t115.5 76.5Q783-687 811.5-622T840-482q0 75-28.5 141t-78 115Q684-177 618-148.5T477-120Zm128-197L451-469v-214h60v189l137 134-43 43Z",
  hourglass_empty: "M308-140h344v-127q0-72-50-121.5T480-438q-72 0-122 49.5T308-267v127Zm294-432q50-50 50-122v-126H308v126q0 72 50 122t122 50q72 0 122-50ZM160-80v-60h88v-127q0-71 40-129t106-84q-66-27-106-85t-40-129v-126h-88v-60h640v60h-88v126q0 71-40 129t-106 85q66 26 106 84t40 129v127h88v60H160Z",
  inbox: "M180-120q-24 0-42-18t-18-42v-600q0-24 18-42t42-18h600q24 0 42 18t18 42v600q0 24-18 42t-42 18H180Zm0-60h600v-136H634q-26 40-67.5 61.5T480-233q-45 0-86.5-21.5T326-316H180v136Zm374-136.5q33-23.5 56-59.5h170v-404H180v404h170q23 36 56.25 59.5 33.24 23.5 74 23.5Q521-293 554-316.5ZM180-180h600-600Z",
  inventory_2: "M180-80q-24.75 0-42.37-17.63Q120-115.25 120-140v-483q-17-6-28.5-21.39T80-680v-140q0-24.75 17.63-42.38Q115.25-880 140-880h680q24.75 0 42.38 17.62Q880-844.75 880-820v140q0 20.22-11.5 35.61T840-623v483q0 24.75-17.62 42.37Q804.75-80 780-80H180Zm0-540v480h600v-480H180Zm-40-60h680v-140H140v140Zm220 260h240v-60H360v60Zm120 40Z",
  key: "M232-432q-20-20-20-48t20-48q20-20 48-20t48 20q20 20 20 48t-20 48q-20 20-48 20t-48-20Zm48 192q-100 0-170-70T40-480q0-100 70-170t170-70q72 0 126 34t85 103h356l113 113-167 153-88-64-88 64-75-60h-51q-25 60-78.5 98.5T280-240Zm0-60q58 0 107-38.5t63-98.5h114l54 45 88-63 82 62 85-79-51-51H450q-12-56-60-96.5T280-660q-75 0-127.5 52.5T100-480q0 75 52.5 127.5T280-300Z",
  language: "M323-111.5Q250-143 196-197t-85-127.5Q80-398 80-482t31-156.5Q142-711 196-765t127-84.5Q396-880 480-880t157 30.5Q710-819 764-765t85 126.5Q880-566 880-482t-31 157.5Q818-251 764-197t-127 85.5Q564-80 480-80t-157-31.5ZM480-138q35-36 58.5-82.5T577-331H384q14 60 37.5 108t58.5 85Zm-85-12q-25-38-43-82t-30-99H172q38 71 88 111.5T395-150Zm171-1q72-23 129.5-69T788-331H639q-13 54-30.5 98T566-151ZM152-391h159q-3-27-3.5-48.5T307-482q0-25 1-44.5t4-43.5H152q-7 24-9.5 43t-2.5 45q0 26 2.5 46.5T152-391Zm221 0h215q4-31 5-50.5t1-40.5q0-20-1-38.5t-5-49.5H373q-4 31-5 49.5t-1 38.5q0 21 1 40.5t5 50.5Zm275 0h160q7-24 9.5-44.5T820-482q0-26-2.5-45t-9.5-43H649q3 35 4 53.5t1 34.5q0 22-1.5 41.5T648-391Zm-10-239h150q-33-69-90.5-115T565-810q25 37 42.5 80T638-630Zm-254 0h194q-11-53-37-102.5T480-820q-32 27-54 71t-42 119Zm-212 0h151q11-54 28-96.5t43-82.5q-75 19-131 64t-91 115Z",
  light_mode: "M579-381q41-41 41-99t-41-99q-41-41-99-41t-99 41q-41 41-41 99t41 99q41 41 99 41t99-41Zm-240.5 42.5Q280-397 280-480t58.5-141.5Q397-680 480-680t141.5 58.5Q680-563 680-480t-58.5 141.5Q563-280 480-280t-141.5-58.5ZM200-450H40v-60h160v60Zm720 0H760v-60h160v60ZM450-760v-160h60v160h-60Zm0 720v-160h60v160h-60ZM262-658l-100-97 43-44 96 100-39 41Zm494 496-98-100 41-41 99 98-42 43Zm-99-537 98-99 44 42-99 98-43-41ZM162-205l99-98 42 42-98 99-43-43Zm318-275Z",
  local_shipping: "M140.5-195.42Q106-229.83 106-279H40v-461q0-24 18-42t42-18h579v167h105l136 181v173h-71q0 49.17-34.38 83.58Q780.24-161 731.12-161t-83.62-34.42Q613-229.83 613-279H342q0 49-34.38 83.5t-83.5 34.5q-49.12 0-83.62-34.42ZM265-238q17-17 17-41t-17-41q-17-17-41-17t-41 17q-17 17-17 41t17 41q17 17 41 17t41-17ZM100-339h22q17-27 43.04-43t58-16q31.96 0 58.46 16.5T325-339h294v-401H100v401Zm672 101q17-17 17-41t-17-41q-17-17-41-17t-41 17q-17 17-17 41t17 41q17 17 41 17t41-17Zm-93-187h186L754-573h-75v148ZM360-529Z",
  lock: "M220-80q-24.75 0-42.37-17.63Q160-115.25 160-140v-434q0-24.75 17.63-42.38Q195.25-634 220-634h70v-96q0-78.85 55.61-134.42Q401.21-920 480.11-920q78.89 0 134.39 55.58Q670-808.85 670-730v96h70q24.75 0 42.38 17.62Q800-598.75 800-574v434q0 24.75-17.62 42.37Q764.75-80 740-80H220Zm0-60h520v-434H220v434Zm314.5-162.03Q557-324.06 557-355q0-30-22.67-54.5t-54.5-24.5q-31.83 0-54.33 24.5t-22.5 55q0 30.5 22.67 52.5t54.5 22q31.83 0 54.33-22.03ZM350-634h260v-96q0-54.17-37.88-92.08-37.88-37.92-92-37.92T388-822.08q-38 37.91-38 92.08v96ZM220-140v-434 434Z",
  logout: "M180-120q-24 0-42-18t-18-42v-600q0-24 18-42t42-18h299v60H180v600h299v60H180Zm486-185-43-43 102-102H360v-60h363L621-612l43-43 176 176-174 174Z",
  mail: "M140-160q-24 0-42-18t-18-42v-520q0-24 18-42t42-18h680q24 0 42 18t18 42v520q0 24-18 42t-42 18H140Zm340-302L140-685v465h680v-465L480-462Zm0-60 336-218H145l335 218ZM140-685v-55 520-465Z",
  menu: "M120-240v-60h720v60H120Zm0-210v-60h720v60H120Zm0-210v-60h720v60H120Z",
  monitoring: "M120-120v-76l60-60v136h-60Zm165 0v-236l60-60v296h-60Zm165 0v-296l60 61v235h-60Zm165 0v-235l60-60v295h-60Zm165 0v-396l60-60v456h-60ZM120-356v-85l280-278 160 160 280-281v85L560-474 400-634 120-356Z",
  more_vert: "M479.86-160Q460-160 446-174.14t-14-34Q432-228 446.14-242t34-14Q500-256 514-241.86t14 34Q528-188 513.86-174t-34 14Zm0-272Q460-432 446-446.14t-14-34Q432-500 446.14-514t34-14Q500-528 514-513.86t14 34Q528-460 513.86-446t-34 14Zm0-272Q460-704 446-718.14t-14-34Q432-772 446.14-786t34-14Q500-800 514-785.86t14 34Q528-732 513.86-718t-34 14Z",
  north_east: "m202-160-42-42 498-498H364v-60h396v396h-60v-294L202-160Z",
  notifications: "M160-200v-60h80v-304q0-84 49.5-150.5T420-798v-22q0-25 17.5-42.5T480-880q25 0 42.5 17.5T540-820v22q81 17 130.5 83.5T720-564v304h80v60H160Zm320-302Zm0 422q-33 0-56.5-23.5T400-160h160q0 33-23.5 56.5T480-80ZM300-260h360v-304q0-75-52.5-127.5T480-744q-75 0-127.5 52.5T300-564v304Z",
  person: "M372-523q-42-42-42-108t42-108q42-42 108-42t108 42q42 42 42 108t-42 108q-42 42-108 42t-108-42ZM160-160v-94q0-38 19-65t49-41q67-30 128.5-45T480-420q62 0 123 15.5T731-360q31 14 50 41t19 65v94H160Zm60-60h520v-34q0-16-9.5-30.5T707-306q-64-31-117-42.5T480-360q-57 0-111 11.5T252-306q-14 7-23 21.5t-9 30.5v34Zm324.5-346.5Q570-592 570-631t-25.5-64.5Q519-721 480-721t-64.5 25.5Q390-670 390-631t25.5 64.5Q441-541 480-541t64.5-25.5ZM480-631Zm0 411Z",
  person_add: "M730-400v-130H600v-60h130v-130h60v130h130v60H790v130h-60ZM252-523q-42-42-42-108t42-108q42-42 108-42t108 42q42 42 42 108t-42 108q-42 42-108 42t-108-42ZM40-160v-94q0-35 17.5-63.5T108-360q75-33 133.5-46.5T360-420q60 0 118 13.5T611-360q33 15 51 43t18 63v94H40Zm60-60h520v-34q0-16-9-30.5T587-306q-71-33-120-43.5T360-360q-58 0-107.5 10.5T132-306q-15 7-23.5 21.5T100-254v34Zm324.5-346.5Q450-592 450-631t-25.5-64.5Q399-721 360-721t-64.5 25.5Q270-670 270-631t25.5 64.5Q321-541 360-541t64.5-25.5ZM360-631Zm0 411Z",
  public: "M324-111.5Q251-143 197-197t-85.5-127Q80-397 80-480t31.5-156Q143-709 197-763t127-85.5Q397-880 480-880t156 31.5Q709-817 763-763t85.5 127Q880-563 880-480t-31.5 156Q817-251 763-197t-127 85.5Q563-80 480-80t-156-31.5ZM437-141v-82q-35 0-59-26t-24-61v-44L149-559q-5 20-7 39.5t-2 39.5q0 130 84.5 227T437-141Zm294-108q44-48 66.5-107.5T820-480q0-106-58-192.5T607-799v18q0 35-24 61t-59 26h-87v87q0 17-13.5 28T393-568h-83v88h258q17 0 28 13t11 30v127h43q29 0 51 17t30 44Z",
  receipt_long: "M222-80q-43.75 0-74.37-30.63Q117-141.25 117-185v-125h127v-570l59.8 60 59.8-60 59.8 60 59.8-60 59.8 60 60-60 60 60 60-60 60 60 60-60v695q0 43.75-30.62 74.37Q781.75-80 738-80H222Zm516-60q20 0 32.5-12.5T783-185v-595H304v470h389v125q0 20 12.5 32.5T738-140ZM357-622v-60h240v60H357Zm0 134v-60h240v60H357Zm333-134q-12 0-21-9t-9-21q0-12 9-21t21-9q12 0 21 9t9 21q0 12-9 21t-21 9Zm0 129q-12 0-21-9t-9-21q0-12 9-21t21-9q12 0 21 9t9 21q0 12-9 21t-21 9ZM221-140h412v-110H177v65q0 20 12.65 32.5T221-140Zm-44 0v-110 110Z",
  restart_alt: "M451-122q-123-10-207-101t-84-216q0-77 35.5-145T295-695l43 43q-56 33-87 90.5T220-439q0 100 66 173t165 84v60Zm60 0v-60q100-12 165-84.5T741-439q0-109-75.5-184.5T481-699h-20l60 60-43 43-133-133 133-133 43 43-60 60h20q134 0 227 93.5T801-439q0 125-83.5 216T511-122Z",
  schedule: "m627-287 45-45-159-160v-201h-60v225l174 181ZM480-80q-82 0-155-31.5t-127.5-86Q143-252 111.5-325T80-480q0-82 31.5-155t86-127.5Q252-817 325-848.5T480-880q82 0 155 31.5t127.5 86Q817-708 848.5-635T880-480q0 82-31.5 155t-86 127.5Q708-143 635-111.5T480-80Zm0-400Zm0 340q140 0 240-100t100-240q0-140-100-240T480-820q-140 0-240 100T140-480q0 140 100 240t240 100Z",
  science: "M172-120q-41.78 0-59.39-39T124-230l248-280v-270h-52q-12.75 0-21.37-8.68-8.63-8.67-8.63-21.5 0-12.82 8.63-21.32 8.62-8.5 21.37-8.5h320q12.75 0 21.38 8.68 8.62 8.67 8.62 21.5 0 12.82-8.62 21.32-8.63 8.5-21.38 8.5h-52v270l248 280q29 32 11.39 71T788-120H172Zm-12-60h640L528-488v-292h-96v292L160-180Zm318-300Z",
  search: "M796-121 533-384q-30 26-70 40.5T378-329q-108 0-183-75t-75-181q0-106 75-181t182-75q106 0 180.5 75T632-585q0 43-14 83t-42 75l264 262-44 44ZM377-389q81 0 138-57.5T572-585q0-81-57-138.5T377-781q-82 0-139.5 57.5T180-585q0 81 57.5 138.5T377-389Z",
  settings: "m388-80-20-126q-19-7-40-19t-37-25l-118 54-93-164 108-79q-2-9-2.5-20.5T185-480q0-9 .5-20.5T188-521L80-600l93-164 118 54q16-13 37-25t40-18l20-127h184l20 126q19 7 40.5 18.5T669-710l118-54 93 164-108 77q2 10 2.5 21.5t.5 21.5q0 10-.5 21t-2.5 21l108 78-93 164-118-54q-16 13-36.5 25.5T592-206L572-80H388Zm48-60h88l14-112q33-8 62.5-25t53.5-41l106 46 40-72-94-69q4-17 6.5-33.5T715-480q0-17-2-33.5t-7-33.5l94-69-40-72-106 46q-23-26-52-43.5T538-708l-14-112h-88l-14 112q-34 7-63.5 24T306-642l-106-46-40 72 94 69q-4 17-6.5 33.5T245-480q0 17 2.5 33.5T254-413l-94 69 40 72 106-46q24 24 53.5 41t62.5 25l14 112Zm44-210q54 0 92-38t38-92q0-54-38-92t-92-38q-54 0-92 38t-38 92q0 54 38 92t92 38Zm0-130Z",
  shield: "M480-81q-140-35-230-162.5T160-523v-238l320-120 320 120v238q0 152-90 279.5T480-81Zm0-62q115-38 187.5-143.5T740-523v-196l-260-98-260 98v196q0 131 72.5 236.5T480-143Zm0-337Z",
  task_alt: "M480-80q-85 0-158-30.5T195-195q-54-54-84.5-127T80-480q0-84 30.5-157T195-764q54-54 127-85t158-31q75 0 140 24t117 66l-43 43q-44-35-98-54t-116-19q-145 0-242.5 97.5T140-480q0 145 97.5 242.5T480-140q145 0 242.5-97.5T820-480q0-30-4.5-58.5T802-594l46-46q16 37 24 77t8 83q0 85-31 158t-85 127q-54 54-127 84.5T480-80Zm-59-218L256-464l45-45 120 120 414-414 46 45-460 460Z",
  verified_user: "m436-347 228-228-42-41-183 183-101-101-44 44 142 143Zm44 266q-140-35-230-162.5T160-523v-238l320-120 320 120v238q0 152-90 279.5T480-81Zm0-62q115-38 187.5-143.5T740-523v-196l-260-98-260 98v196q0 131 72.5 236.5T480-143Zm0-337Z",
  visibility: "M600.5-379.5Q650-429 650-500t-49.5-120.5Q551-670 480-670t-120.5 49.5Q310-571 310-500t49.5 120.5Q409-330 480-330t120.5-49.5Zm-200-41Q368-453 368-500t32.5-79.5Q433-612 480-612t79.5 32.5Q592-547 592-500t-32.5 79.5Q527-388 480-388t-79.5-32.5ZM216-283Q98-366 40-500q58-134 176-217t264-83q146 0 264 83t176 217q-58 134-176 217t-264 83q-146 0-264-83Zm264-217Zm222.5 174.5Q804-391 857-500q-53-109-154.5-174.5T480-740q-121 0-222.5 65.5T102-500q54 109 155.5 174.5T480-260q121 0 222.5-65.5Z",
  visibility_off: "m629-419-44-44q26-71-27-118t-115-24l-44-44q17-11 38-16t43-5q71 0 120.5 49.5T650-500q0 22-5.5 43.5T629-419Zm129 129-40-40q49-36 85.5-80.5T857-500q-50-111-150-175.5T490-740q-42 0-86 8t-69 19l-46-47q35-16 89.5-28T485-800q143 0 261.5 81.5T920-500q-26 64-67 117t-95 93Zm58 226L648-229q-35 14-79 21.5t-89 7.5q-146 0-265-81.5T40-500q20-52 55.5-101.5T182-696L56-822l42-43 757 757-39 44ZM223-654q-37 27-71.5 71T102-500q51 111 153.5 175.5T488-260q33 0 65-4t48-12l-64-64q-11 5-27 7.5t-30 2.5q-70 0-120-49t-50-121q0-15 2.5-30t7.5-27l-97-97Zm305 142Zm-116 58Z",
};

// ---- small DOM helpers ----
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el) el[key] = value;
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({
    width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
  })) svg.setAttribute(k, v);
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

function mi(name, label) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 -960 960 960");
  svg.setAttribute("class", "mi");
  if (label) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label); } else svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", MI[name] || "");
  svg.append(path);
  return svg;
}

// The brand: "TW" on a dark square, then the name. Size comes from the CSS class.
const BRAND = "TW-ai";
function logo(size = "") {
  return h("span", { class: `logo ${size}`, "aria-hidden": "true" }, "TW");
}
function brand(size = "") {
  return [logo(size), h("span", { class: "brand-text" }, h("span", { class: "brand-name" }, BRAND), h("span", { class: "brand-by" }, "by TW Labs"))];
}

// ---- theme: the system's, unless the person picked one (kept in this browser only) ----
function savedTheme() {
  try { return localStorage.getItem("theme"); } catch { return null; }
}
function currentTheme() {
  const picked = document.documentElement.dataset.theme;
  if (picked === "light" || picked === "dark") return picked;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
applyTheme(savedTheme());
function themeButton(extraClass = "") {
  const button = h("button", { type: "button", class: `icon-button theme-toggle ${extraClass}` });
  const paint = () => {
    const dark = currentTheme() === "dark";
    button.replaceChildren(mi(dark ? "light_mode" : "dark_mode"));
    button.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    button.title = button.getAttribute("aria-label");
  };
  button.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem("theme", next); } catch { /* this browser won't remember it */ }
    document.querySelectorAll(".theme-toggle").forEach((b) => b.dispatchEvent(new Event("repaint")));
  });
  button.addEventListener("repaint", paint);
  paint();
  return button;
}

// Whether the server and its model are up (GET /readyz, public), for the status lines.
async function serverStatus() {
  try {
    const response = await fetch("/readyz");
    const data = await response.json();
    return { up: !!data.ready, model: data.checks?.model_server === "ok" };
  } catch {
    return { up: false, model: false };
  }
}

function initials(name) {
  return (name || "?").trim().slice(0, 1).toUpperCase();
}

function show(...nodes) {
  root.replaceChildren(...nodes);
}

// replaceChildren would print `false` from a `condition && node` as text.
function fill(el, ...nodes) {
  el.replaceChildren(...nodes.flat().filter(Boolean));
}

function outcomePill(outcome) {
  const [text, kind] = OUTCOMES[outcome] || [outcome, ""];
  return h("span", { class: `pill ${kind}` }, text);
}

function errorBox() {
  const box = h("div", { class: "notice error", hidden: true, role: "alert" });
  box.fail = (err) => {
    box.textContent = err.message || String(err);
    box.hidden = false;
  };
  box.clear = () => { box.hidden = true; };
  return box;
}

function table(headers, rows, emptyText) {
  if (!rows.length) return h("div", { class: "empty" }, emptyText);
  const align = (index) => (typeof rows[0][index] === "number" ? "num" : null);
  return h("div", { class: "table-wrap" }, h("table", null,
    h("thead", null, h("tr", null, headers.map((text, i) => h("th", { scope: "col", class: align(i) }, text)))),
    h("tbody", null, rows.map((cells) => h("tr", null, cells.map((cell, i) => h("td", { class: align(i) }, cell))))),
  ));
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}


// ---- Markdown, rendered safely ----
// Replies come from the model and from web pages it read, so they are never put into innerHTML:
// this builds elements directly. Links open only http(s) and mailto, in a new tab.
const MD_BLOCK_START = /^\s*(```|~~~|#{1,6}\s|>|([-*+]|\d+[.)])\s+)/;
const MD_LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const MD_INLINE = /(`+)([^`]+?)\1|\*\*([^*]+?)\*\*|__([^_]+?)__|\*([^*\s][^*]*?)\*|(?<![\w])_([^_\s][^_]*?)_(?![\w])|~~([^~]+?)~~|\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

function mdLink(href, label) {
  if (!/^(https?:|mailto:)/i.test(href)) return label;
  return h("a", { href, target: "_blank", rel: "noopener noreferrer nofollow" }, label);
}

function mdInline(text) {
  const frag = document.createDocumentFragment();
  const re = new RegExp(MD_INLINE.source, "g");
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    if (m[2] !== undefined) frag.append(h("code", null, m[2]));
    else if (m[3] !== undefined || m[4] !== undefined) frag.append(h("strong", null, mdInline(m[3] ?? m[4])));
    else if (m[5] !== undefined || m[6] !== undefined) frag.append(h("em", null, mdInline(m[5] ?? m[6])));
    else if (m[7] !== undefined) frag.append(h("del", null, mdInline(m[7])));
    else if (m[8] !== undefined) frag.append(mdLink(m[9], mdInline(m[8])));
    else if (m[10] !== undefined) frag.append(mdLink(m[10], m[10]));
    last = re.lastIndex;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

function mdList(lines, start) {
  const first = lines[start].match(MD_LIST_ITEM);
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const el = h(ordered ? "ol" : "ul");
  if (ordered && parseInt(first[2], 10) !== 1) el.start = parseInt(first[2], 10);
  let i = start;
  let li = null;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) j++;
      const next = j < lines.length && lines[j].match(MD_LIST_ITEM);
      if (next && next[1].length >= indent) { i = j; continue; }
      break;
    }
    const m = line.match(MD_LIST_ITEM);
    const lead = line.match(/^\s*/)[0].length;
    if (m && m[1].length === indent) {
      li = h("li", null, mdInline(m[3]));
      el.append(li);
      i++;
    } else if (m && m[1].length > indent && li) {
      const [sub, next] = mdList(lines, i);
      li.append(sub);
      i = next;
    } else if (!m && lead > indent && li) {
      li.append(h("br"), mdInline(line.trim()));
      i++;
    } else {
      break;
    }
  }
  return [el, i];
}

function md(source) {
  const out = document.createDocumentFragment();
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    let m = line.match(/^\s*(```|~~~)\s*([\w+-]*)\s*$/);
    if (m) {
      const code = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(m[1])) code.push(lines[i++]);
      i++;
      out.append(h("pre", { class: "md-code" }, h("code", null, code.join("\n"))));
      continue;
    }
    m = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (m) {
      out.append(h(["h3", "h4", "h5", "h5", "h6", "h6"][m[1].length - 1], null, mdInline(m[2])));
      i++;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.append(h("hr")); i++; continue; }
    if (/^\s*>/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.append(h("blockquote", null, md(quote.join("\n"))));
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      const head = cells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      out.append(h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, head.map((c) => h("th", null, mdInline(c))))),
        h("tbody", null, rows.map((r) => h("tr", null, r.map((c) => h("td", null, mdInline(c)))))),
      )));
      continue;
    }
    if (MD_LIST_ITEM.test(line)) {
      const [list, next] = mdList(lines, i);
      out.append(list);
      i = next;
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && (!para.length || !MD_BLOCK_START.test(lines[i]))) para.push(lines[i++]);
    const p = h("p");
    para.forEach((l, k) => { if (k) p.append(h("br")); p.append(mdInline(l.trim())); });
    out.append(p);
  }
  return out;
}

function mdBlock(text, extraClass = "") {
  return h("div", { class: `md ${extraClass}` }, md(text));
}

// ---- API ----
async function api(path, { method = "GET", body, form, raw } = {}) {
  const headers = {};
  if (state.token) headers.authorization = `Bearer ${state.token}`;
  if (body) headers["content-type"] = "application/json";
  const response = await fetch(path, {
    method, headers, body: body ? JSON.stringify(body) : form,
  });
  if (response.status === 401 && state.token && path !== "/v1/auth/change-password"
      && path !== "/v1/auth/recovery-code") {
    signOut("Your session ended. Sign in again.");
    throw new Error("Session ended");
  }
  if (!response.ok) {
    let detail = `The server returned an error (${response.status}). Try again.`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") detail = data.detail;
      else if (Array.isArray(data.detail)) detail = data.detail.map((d) => d.msg).join("; ");
    } catch { /* keep the generic message */ }
    throw new Error(detail);
  }
  if (raw) return response.blob();
  return response.status === 204 ? null : response.json();
}

// Calls onDelta with each piece of the reply as it arrives. Resolves with the finished reply.
async function streamChat(body, onDelta, onStep = () => {}) {
  const response = await fetch("/v1/chat/stream", {
    method: "POST",
    headers: { authorization: `Bearer ${state.token}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 401) {
    signOut("Your session ended. Sign in again.");
    throw new Error("Session ended");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : `The server returned an error (${response.status}). Try again.`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const line = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      if (!line.startsWith("data: ")) continue;
      const event = JSON.parse(line.slice(6));
      if (event.type === "delta") onDelta(event.text);
      else if (event.type === "step") onStep(event.text);
      else if (event.type === "error") throw new Error(event.detail);
      else if (event.type === "done") finished = event;
    }
  }
  if (!finished) throw new Error("The reply was cut off. Try again.");
  return finished;
}

function useToken(result, username) {
  state.token = result.access_token;
  const payload = result.access_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  // For choosing which pages to show until /v1/dashboard answers. The server checks every action itself.
  state.roles = JSON.parse(atob(payload)).roles || [];
  state.username = username;
}

function signOut(message) {
  clearInterval(state.poll);
  clearInterval(state.bellPoll);
  Object.assign(state, {
    token: null, username: "", roles: [], view: "chat", thread: null, threads: [], counts: {}, poll: null,
    bellPoll: null, unread: 0,
  });
  loginScreen(message);
}

const has = (...roles) => roles.some((role) => state.roles.includes(role));

// ---- sign-in screens ----
// An input with an icon in front (and a show/hide button for passwords).
function iconField(iconName, input) {
  const parts = [mi(iconName), input];
  if (input.type === "password") {
    const reveal = h("button", { type: "button", class: "field-end", "aria-label": "Show password", title: "Show password" }, mi("visibility"));
    reveal.addEventListener("click", () => {
      const hidden = input.type === "password";
      input.type = hidden ? "text" : "password";
      reveal.replaceChildren(mi(hidden ? "visibility_off" : "visibility"));
      reveal.setAttribute("aria-label", hidden ? "Hide password" : "Show password");
    });
    parts.push(reveal);
  }
  return h("div", { class: "field" }, parts);
}

// The frame around every sign-in screen: theme switch in the corner, live status under the card.
function authFrame(card) {
  const status = h("div", { class: "auth-status" });
  serverStatus().then(({ up, model }) => fill(status,
    h("span", null, h("span", { class: `status-dot ${up ? "up" : "down"}` }), "System: ", h("strong", null, up ? "Operational" : "Unavailable")),
    up && h("span", null, "Assistant: ", h("strong", null, model ? "Online" : "Starting")),
  ));
  return h("div", { class: "center" }, h("div", { class: "corner" }, themeButton("boxed")), card, status);
}

function loginScreen(message) {
  const error = errorBox();
  const username = h("input", { autocomplete: "username", required: true, placeholder: "Your username", id: "login-username" });
  const password = h("input", { type: "password", autocomplete: "current-password", required: true, placeholder: "••••••••••••", id: "login-password" });
  const submit = h("button", { class: "primary wide", type: "submit" }, "Sign in", mi("arrow_forward"));
  show(authFrame(h("form", {
    class: "card auth",
    onsubmit: async (event) => {
      event.preventDefault();
      error.clear();
      submit.disabled = true;
      try {
        const result = await api("/v1/auth/login", {
          method: "POST", body: { username: username.value, password: password.value },
        });
        useToken(result, username.value);
        if (result.must_change_password) firstPasswordScreen(password.value);
        else shell();
      } catch (err) {
        error.fail(err);
        submit.disabled = false;
      }
    },
  },
    h("div", { class: "auth-brand" }, brand("lg")),
    h("h1", null, "Welcome back"),
    h("p", { class: "auth-sub" }, "Sign in to your private workspace"),
    message && h("div", { class: "notice info" }, message),
    error,
    h("label", { for: "login-username" }, h("span", { class: "caps" }, "Username"), iconField("alternate_email", username)),
    h("label", { for: "login-password" },
      h("span", { class: "label-row" }, h("span", { class: "caps" }, "Password"),
        h("button", { class: "link small", type: "button", onclick: recoverScreen }, "Forgot password?")),
      iconField("lock", password)),
    h("div", { class: "auth-actions" }, submit),
    h("p", { class: "auth-foot" }, mi("lock"), "Private chats are encrypted with a key only you can unlock."),
  )));
  username.focus();
}

function firstPasswordScreen(oldPassword) {
  const error = errorBox();
  const next = h("input", { type: "password", autocomplete: "new-password", minLength: 12, required: true });
  const again = h("input", { type: "password", autocomplete: "new-password", required: true });
  show(authFrame(h("form", {
    class: "card auth",
    onsubmit: async (event) => {
      event.preventDefault();
      error.clear();
      if (next.value !== again.value) return error.fail(new Error("The passwords don't match."));
      try {
        const result = await api("/v1/auth/change-password", {
          method: "POST", body: { old_password: oldPassword, new_password: next.value },
        });
        useToken(result, state.username);
        recoveryCodeScreen(result.recovery_code);
      } catch (err) {
        error.fail(err);
      }
    },
  },
    h("div", { class: "auth-brand" }, brand("lg")),
    h("h1", null, "Set your own password"),
    h("p", { class: "muted" }, "Your administrator chose your first password. Set your own to start chatting. "
      + "Only your password can unlock your private chats, so your administrator can't read them."),
    error,
    h("label", null, "New password (at least 12 characters)", next),
    h("label", null, "New password again", again),
    h("button", { class: "primary", type: "submit" }, "Save password"),
  )));
  next.focus();
}

function recoveryCodeScreen(code) {
  const saved = h("input", { type: "checkbox" });
  const proceed = h("button", { class: "primary", disabled: true, onclick: shell }, "Continue");
  saved.addEventListener("change", () => { proceed.disabled = !saved.checked; });
  const copy = h("button", {
    type: "button",
    onclick: async () => {
      await navigator.clipboard.writeText(code);
      copy.textContent = "Copied";
    },
  }, "Copy code");
  show(authFrame(h("div", { class: "card auth" },
    h("h1", null, "Save your recovery code"),
    h("p", null, "If you forget your password, this code is the only way back into your private chats. "
      + "It won't be shown again, and nobody can reset it for you."),
    h("div", { class: "code", id: "recovery-code" }, code),
    h("p", null, copy),
    h("div", { class: "notice warn" }, "Keep it somewhere private. Anyone with this code can read your private chats."),
    h("label", { class: "check" }, saved, h("span", null, "I have saved my recovery code")),
    proceed,
  )));
}

function recoverScreen() {
  const error = errorBox();
  const username = h("input", { autocomplete: "username", required: true });
  const code = h("input", { autocomplete: "off", required: true, placeholder: "ABCD-EFGH-..." });
  const next = h("input", { type: "password", autocomplete: "new-password", minLength: 12, required: true });
  show(authFrame(h("form", {
    class: "card auth",
    onsubmit: async (event) => {
      event.preventDefault();
      error.clear();
      try {
        const result = await api("/v1/auth/recover", {
          method: "POST",
          body: { username: username.value, recovery_code: code.value, new_password: next.value },
        });
        useToken(result, username.value);
        recoveryCodeScreen(result.recovery_code);
      } catch (err) {
        error.fail(err);
      }
    },
  },
    h("div", { class: "auth-brand" }, brand("lg")),
    h("h1", null, "Recover your account"),
    h("p", { class: "muted" }, "Your recovery code sets a new password and keeps your chats. "
      + "The code works once, and you'll get a new one."),
    error,
    h("label", null, "Username", username),
    h("label", null, "Recovery code", code),
    h("label", null, "New password (at least 12 characters)", next),
    h("div", { class: "row spread" },
      h("button", { class: "primary", type: "submit" }, "Recover account"),
      h("button", { class: "link", type: "button", onclick: () => loginScreen() }, "Back to sign in"),
    ),
  )));
  username.focus();
}

// ---- shell ----
// [id, label, view, badge count key, icon, group]
function morePages() {
  return [
    ["overview", "Overview", overviewView, null, "dashboard", "work"],
    has("admin") && ["approvals", "Approvals", approvalsView, "pending_approvals", "gavel", "work"],
    has("admin", "account") && ["inbox", "Accounts inbox", inboxView, "inbox_queued", "inbox", "work"],
    has("admin", "hr", "account") && ["reports", "Reports", reportsView, "open_reports", "bar_chart", "work"],
    has("admin") && ["users", "Users", usersView, null, "group", "work"],
    has("admin") && ["credentials", "Credentials", credentialsView, null, "key", "work"],
    ["templates", "Templates", templatesView, null, "description", "work"],
    ["files", "My files", filesView, null, "folder", "mine"],
    ["reminders", "Reminders", remindersView, null, "alarm", "mine"],
    ["notifications", "Notifications", notificationsView, "unread_notifications", "notifications", "mine"],
    ["account", "Settings", accountView, null, "settings", "mine"],
  ].filter(Boolean);
}

function newThreadId() {
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

async function refreshCounts() {
  try {
    state.counts = { ...(await api("/v1/dashboard")), unread_notifications: state.unread };
    state.roles = state.counts.roles;
  } catch { /* the page itself reports errors */ }
  return state.counts;
}

// The bell: checks for new notifications every minute, and pops up a browser notification for
// new ones while this tab is open (if the person allowed it under Notifications).
async function checkBell(paint) {
  try {
    const { unread } = await api("/v1/notifications/count");
    if (unread > state.unread && "Notification" in window && Notification.permission === "granted") {
      const fresh = await api("/v1/notifications?unread_only=true");
      fresh.slice(0, unread - state.unread).reverse().forEach((n) => {
        try { new Notification(n.title, { body: n.body.slice(0, 200), tag: n.id }); } catch { /* not supported here */ }
      });
    }
    state.unread = unread;
    state.counts.unread_notifications = unread;
    if (state.token) paint();
  } catch { /* try again next time */ }
}

function shell() {
  const view = h("div", { class: "view" });
  const chats = h("div", { class: "chats", "aria-label": "Your chats" });
  const work = h("div", { class: "side-more" });
  const mine = h("div", { class: "side-more" });
  const side = h("nav", { class: "side", "aria-label": "Main" });
  const scrim = h("button", { class: "scrim", "aria-label": "Close menu", onclick: () => toggleSide(false) });
  const bellDot = h("span", { class: "dot", hidden: true });
  const toggleSide = (open) => {
    side.classList.toggle("open", open);
    scrim.classList.toggle("open", open);
  };

  const pageItem = ([id, label, , badge, iconName]) => {
    const count = badge && state.counts[badge];
    return h("button", {
      class: `side-item ${state.view === id ? "active" : ""}`,
      "aria-current": state.view === id ? "page" : null,
      onclick: () => go(id),
    }, h("span", { class: "lead" }, mi(iconName), h("span", { class: "title" }, label)), count > 0 && h("span", { class: "badge" }, count));
  };

  const paint = () => {
    fill(chats,
      !state.threads.length && h("div", { class: "chats-empty" }, "Your chats will appear here."),
      state.threads.map((t) => h("button", {
        class: `side-item ${state.view === "chat" && t.thread_id === state.thread ? "active" : ""}`,
        onclick: () => openChat(t.thread_id),
      }, h("span", { class: "lead" }, mi("chat_bubble"), h("span", { class: "title", title: t.title }, t.title || "Untitled chat")))),
    );
    const pages = morePages();
    fill(work,
      h("button", {
        class: `side-item ${state.view === "chat" && !state.threads.some((t) => t.thread_id === state.thread) ? "active" : ""}`,
        onclick: () => openChat(newThreadId()),
      }, h("span", { class: "lead" }, mi("forum"), h("span", { class: "title" }, "Chat assistant"))),
      pages.filter((p) => p[5] === "work").map(pageItem),
    );
    fill(mine, pages.filter((p) => p[5] === "mine").map(pageItem));
    bellDot.hidden = !(state.unread > 0);
  };

  const reloadThreads = async () => {
    try {
      state.threads = await api("/v1/chat/threads");
    } catch (err) {
      state.threads = [];
      if (!err.message.includes("change your password")) throw err;
    }
    paint();
  };

  const go = async (id) => {
    clearInterval(state.poll);
    state.view = id;
    toggleSide(false);
    paint();
    const [, , render] = morePages().find(([page]) => page === id);
    view.replaceChildren(h("div", { class: "page muted" }, "Loading..."));
    try {
      const content = await render(() => go(id));
      view.replaceChildren(h("div", { class: "page" }, content));
    } catch (err) {
      if (state.token) view.replaceChildren(h("div", { class: "page" }, h("div", { class: "notice error", role: "alert" }, err.message)));
    }
    refreshCounts().then(() => { if (state.token) paint(); });
  };

  const openChat = async (threadId) => {
    clearInterval(state.poll);
    state.view = "chat";
    state.thread = threadId;
    toggleSide(false);
    paint();
    try {
      view.replaceChildren(await chatView(threadId, { reloadThreads, paint }));
    } catch (err) {
      if (state.token) view.replaceChildren(h("div", { class: "page" }, h("div", { class: "notice error", role: "alert" }, err.message)));
    }
  };

  state.go = go;
  fill(side,
    h("div", { class: "side-top" }, brand()),
    h("div", { class: "side-body" },
      h("button", { class: "new-chat", onclick: () => openChat(newThreadId()) },
        h("span", null, mi("add"), "New chat")),
      h("div", { class: "side-label" }, "Recents"),
      chats,
      h("div", { class: "side-label" }, "Workspace"),
      work,
      h("div", { class: "side-label" }, "Personal"),
      mine,
    ),
    h("div", { class: "who" },
      h("span", { class: "avatar", "aria-hidden": "true" }, initials(state.username)),
      h("span", { class: "who-text" },
        h("span", { class: "who-name" }, state.username),
        h("span", { class: "who-role" }, state.roles.map(departmentName).join(" · ") || "Member"),
      ),
      h("button", { class: "icon-button", "aria-label": "Sign out", title: "Sign out", onclick: () => signOut() }, mi("logout")),
    ),
  );
  show(h("div", { class: "shell" },
    side,
    scrim,
    h("div", { class: "main" },
      h("header", { class: "topbar" },
        h("button", { class: "icon-button menu-button", "aria-label": "Open menu", onclick: () => toggleSide(true) }, mi("menu")),
        h("span", { class: "top-brand" }, brand()),
        searchBox({ openChat, go }),
        h("div", { class: "top-actions" },
          themeButton(),
          h("button", { class: "icon-button", "aria-label": "Notifications", title: "Notifications", onclick: () => go("notifications") },
            mi("notifications"), bellDot),
          h("button", { class: "icon-button boxed", "aria-label": "Settings", title: "Your account and settings", onclick: () => go("account") },
            mi("person")),
        ),
      ),
      view,
    ),
  ));
  reloadThreads().catch(() => {}).then(() => {
    openChat(newThreadId());
    refreshCounts().then(() => { if (state.token) paint(); });
  });
  clearInterval(state.bellPoll);
  checkBell(paint);
  state.bellPoll = setInterval(() => { if (state.token) checkBell(paint); }, 60000);
}

// "Search workspace": jump to a page or one of your chats by name. Ctrl/⌘+K focuses it.
function searchBox({ openChat, go }) {
  const input = h("input", { type: "search", placeholder: "Search chats and pages...", "aria-label": "Search chats and pages", autocomplete: "off" });
  const results = h("div", { class: "search-results", role: "listbox", hidden: true });
  let picked = 0;
  let found = [];
  const close = () => { results.hidden = true; };
  const choose = (item) => { close(); input.value = ""; input.blur(); item.run(); };
  const paint = () => {
    const q = input.value.trim().toLowerCase();
    const pages = morePages().map(([id, label, , , iconName]) => ({ label, iconName, group: "Pages", run: () => go(id) }));
    const chats = state.threads.map((t) => ({ label: t.title || "Untitled chat", iconName: "chat_bubble", group: "Chats", run: () => openChat(t.thread_id) }));
    found = [...pages, ...chats].filter((item) => !q || item.label.toLowerCase().includes(q)).slice(0, 12);
    picked = Math.min(picked, Math.max(0, found.length - 1));
    let group = "";
    fill(results, found.length ? found.flatMap((item, i) => {
      const head = item.group !== group ? h("div", { class: "group" }, (group = item.group)) : null;
      return [head, h("button", {
        type: "button", role: "option", class: `side-item ${i === picked ? "picked" : ""}`,
        onmousedown: (event) => { event.preventDefault(); choose(item); },
      }, h("span", { class: "lead" }, mi(item.iconName), h("span", { class: "title" }, item.label)))];
    }) : h("div", { class: "none" }, "Nothing matches."));
    results.hidden = false;
  };
  input.addEventListener("focus", paint);
  input.addEventListener("input", () => { picked = 0; paint(); });
  input.addEventListener("blur", () => setTimeout(close, 100));
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      picked = (picked + (event.key === "ArrowDown" ? 1 : -1) + found.length) % Math.max(1, found.length);
      paint();
    } else if (event.key === "Enter" && found[picked]) {
      event.preventDefault();
      choose(found[picked]);
    } else if (event.key === "Escape") {
      close();
      input.blur();
    }
  });
  if (!searchBox.bound) {
    searchBox.bound = true;
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        const box = document.querySelector(".search input");
        if (box) { event.preventDefault(); box.focus(); }
      }
    });
  }
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  return h("div", { class: "search" }, iconField("search", input), h("kbd", null, mac ? "⌘K" : "Ctrl K"), results);
}

// A page title in the serif display face, with an optional mono "eyebrow" trail above it, a chip
// beside it and actions on the right.
function heading(text, subtitle, { eyebrow, chip, actions } = {}) {
  return h("div", { class: "head" },
    eyebrow && h("div", { class: "eyebrow" }, eyebrow.flatMap((part, i) => [
      i > 0 && h("span", { class: "sep" }, "/"),
      i === eyebrow.length - 1 ? h("strong", null, part) : h("span", null, part),
    ])),
    h("div", { class: "head-row" },
      h("div", null, h("h1", null, text, chip), subtitle && h("p", { class: "muted" }, subtitle)),
      actions && h("div", { class: "row" }, actions),
    ),
  );
}

// ---- chat ----
async function chatView(thread, { reloadThreads, paint }) {
  const known = () => state.threads.some((t) => t.thread_id === thread);
  const messages = h("div", { class: "messages column", "aria-live": "polite" });
  const requests = h("div", { class: "reqs column" });
  const scroll = h("div", { class: "scroll" }, messages, requests);
  const files = h("div", { class: "files" });
  const error = errorBox();
  const input = h("textarea", { placeholder: "Ask anything: personal questions stay private, work goes to the right team...", "aria-label": "Message", rows: 2 });
  const send = h("button", { class: "primary round", type: "submit", "aria-label": "Send", title: "Send (Enter)" }, mi("arrow_upward"));
  const picker = h("input", { type: "file", class: "hidden-input", id: "file-picker", onchange: () => upload() });

  const toBottom = () => { scroll.scrollTop = scroll.scrollHeight; };
  const setEmpty = (empty) => content.classList.toggle("is-empty", empty);

  const bubble = (role, text, handledBy, dashboards) => {
    const node = h("div", { class: `msg ${role} ${role === "assistant" ? "md" : ""}` }, role === "assistant" ? md(text) : text,
      role === "assistant" && handledBy && h("span", { class: "handled" }, mi("task_alt"), `Handled as work by ${departmentName(handledBy)}`));
    messages.append(node);
    for (const dash of dashboards || []) messages.append(renderDashboard(dash));
    toBottom();
    return node;
  };

  const paintFiles = async () => {
    const items = await api(`/v1/chat/attachments?thread_id=${encodeURIComponent(thread)}`);
    files.replaceChildren(...items.map((item) => h("div", { class: "file" },
      h("span", { title: item.name }, `${item.name} (${formatSize(item.size)})`),
      h("button", {
        type: "button",
        onclick: async () => {
          const blob = await api(`/v1/chat/attachments/${item.id}`, { raw: true });
          const link = h("a", { href: URL.createObjectURL(blob), download: item.name });
          link.click();
          URL.revokeObjectURL(link.href);
        },
      }, "Download"),
      h("button", {
        type: "button",
        class: "danger",
        "aria-label": `Delete ${item.name}`,
        onclick: async () => {
          await api(`/v1/chat/attachments/${item.id}`, { method: "DELETE" });
          paintFiles();
        },
      }, "Delete"),
    )));
  };

  const paintRequests = async () => {
    const items = await api(`/v1/chat/threads/${encodeURIComponent(thread)}/requests`);
    fill(requests, items.map((item) => {
      const [text, kind] = REQUEST_STATES[item.state] || [item.state, ""];
      return h("div", { class: "req" },
        h("div", { class: "row spread" },
          h("span", null, `${REQUEST_LABELS[item.label] || item.label} (${departmentName(item.department)})`),
          h("span", { class: `pill ${kind}` }, text),
        ),
        item.result && mdBlock(item.result),
      );
    }));
    return items.some((item) => !FINISHED.has(item.state));
  };

  // The assistant asks before sending mail or deleting a file in a private chat.
  const confirmCard = (pending) => {
    const error = errorBox();
    const card = h("div", { class: "confirm card" });
    const decide = (approve, asWork = false) => async () => {
      error.clear();
      card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        const result = await api(`/v1/chat/threads/${encodeURIComponent(thread)}/confirm`, { method: "POST", body: { approve, as_work: asWork } });
        card.remove();
        showResult(result);
      } catch (err) {
        error.fail(err);
        card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      }
    };
    const LABELS = { to: "To", subject: "Subject", body: "Message", path: "File", category: "Mailbox", in_reply_to: "In reply to",
      summary: "What happens", label: "Button" };
    delete pending.details.ref;
    fill(card,
      h("strong", null, pending.title),
      h("dl", { class: "facts" }, Object.entries(pending.details).flatMap(([k, v]) => [
        h("dt", null, LABELS[k] || k), h("dd", { class: k === "body" ? "pre" : null }, v),
      ])),
      error,
      pending.purchase && h("p", { class: "small muted" }, "Is this for personal use or for work? Work orders go to the Accounts "
        + "team, who pay with the company's details."),
      pending.purchase
        ? h("div", { class: "row" },
          h("button", { class: "primary", type: "button", onclick: decide(true) }, "Personal: place it"),
          h("button", { type: "button", onclick: decide(false, true) }, "Work: send to Accounts"),
          h("button", { type: "button", onclick: decide(false) }, "Cancel"))
        : h("div", { class: "row" },
          h("button", { class: "primary", type: "button", onclick: decide(true) },
            pending.tool === "send_mail" ? "Send" : pending.tool === "browser_commit" ? "Yes, go ahead" : "Yes, go ahead"),
          h("button", { type: "button", onclick: decide(false) }, "No")),
    );
    messages.append(card);
    toBottom();
  };

  const say = (text) => { input.value = text; grow(); composer.requestSubmit(); };
  const secretBox = (scope, ask) => {
    const value = h("input", { type: "password", autocomplete: "one-time-code", "aria-label": ask.what });
    const reveal = h("input", { type: "checkbox", onchange: () => { value.type = reveal.checked ? "text" : "password"; } });
    const err = errorBox();
    const box = h("form", {
      class: "card confirm",
      onsubmit: async (event) => {
        event.preventDefault();
        err.clear();
        try {
          await api(`/v1/browser/${encodeURIComponent(scope)}/secret`, { method: "POST", body: { ref: ask.ref, value: value.value, site: ask.site } });
          box.replaceChildren(h("div", { class: "notice good" }, `Entered on ${ask.site}.`));
          say("I've entered it, please carry on.");
        } catch (e) { err.fail(e); }
      },
    },
      h("strong", null, icon("lock"), ` ${ask.what}`),
      h("p", { class: "small muted" }, `Typed straight into the page on ${ask.site}. It isn't saved, shown to the assistant, or kept in this chat.`),
      err,
      h("div", { class: "row" }, value, h("button", { class: "primary", type: "submit" }, "Enter")),
      h("label", { class: "check small" }, reveal, h("span", null, "Show")),
    );
    messages.append(box);
    toBottom();
    value.focus();
  };
  const openBrowser = (scope, reason) => {
    content.querySelectorAll(".live").forEach((el) => el.remove());
    messages.append(browserPanel(scope, reason, { onDone: () => say("Done, please carry on.") }));
    setEmpty(false);
    toBottom();
  };

  const handover = (b) => {
    if (!b) return;
    if (b.secret) secretBox(b.scope, b.secret);
    else openBrowser(b.scope, b.reason);
  };

  const showResult = (result) => {
    if (result.outcome === "confirm") confirmCard(result.confirm);
    else if (result.reply) bubble("assistant", result.reply);
    handover(result.browser);
  };

  const watchRequests = async () => {
    clearInterval(state.poll);
    if (await paintRequests()) {
      state.poll = setInterval(async () => {
        if (!state.token || state.thread !== thread || !(await paintRequests())) clearInterval(state.poll);
      }, 10000);
    }
  };

  const upload = async () => {
    const file = picker.files[0];
    if (!file) return;
    error.clear();
    const form = new FormData();
    form.append("thread_id", thread);
    form.append("file", file);
    try {
      const result = await api("/v1/chat/attachments", { method: "POST", form });
      if (result.outcome === "flagged_nsfw") {
        setEmpty(false);
        bubble("flagged", `"${file.name}" was blocked and reported to HR and the admin.`);
      }
      await paintFiles();
    } catch (err) {
      error.fail(err);
    }
    picker.value = "";
  };

  const grow = () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  };

  const composer = h("form", {
    class: "composer",
    onsubmit: async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text || send.disabled) return;
      error.clear();
      input.value = "";
      grow();
      send.disabled = true;
      setEmpty(false);
      const mine = bubble("user", text);
      const thinking = h("div", { class: "msg assistant thinking" }, "Thinking...");
      messages.append(thinking);
      toBottom();
      // What the agents are doing, as it happens: earlier steps get a tick, the current one pulses.
      const steps = h("ol", { class: "steps", "aria-live": "polite" });
      const onStep = (textOfStep) => {
        if (!steps.isConnected) {
          thinking.classList.remove("thinking");
          thinking.replaceChildren(steps);
        }
        steps.querySelectorAll("li.now").forEach((li) => li.classList.replace("now", "done"));
        steps.append(h("li", { class: "now" }, textOfStep));
        toBottom();
      };
      try {
        let started = false;
        let streamed = "";
        let drawing = false;
        const result = await streamChat({ message: text, thread_id: thread }, (piece) => {
          if (!started) {
            started = true;
            thinking.classList.remove("thinking");
            thinking.classList.add("md");
          }
          streamed += piece;
          if (!drawing) {  // at most one redraw per frame
            drawing = true;
            requestAnimationFrame(() => { drawing = false; thinking.replaceChildren(md(streamed)); toBottom(); });
          }
        }, onStep);
        thinking.remove();
        if (result.outcome === "flagged_nsfw") {
          mine.remove();
          bubble("flagged", "This message was blocked and reported to HR and the admin. It wasn't saved in this chat.");
        } else if (result.outcome === "confirm") {
          confirmCard(result.confirm);
          await reloadThreads();
        } else {
          bubble("assistant", result.reply, result.route === "work" ? result.department : null, result.dashboards);
          handover(result.browser);
          await reloadThreads();
          if (result.route === "work") await watchRequests();
          toBottom();
        }
      } catch (err) {
        thinking.remove();
        mine.remove();
        input.value = text;
        grow();
        error.fail(err);
        if (!messages.children.length) setEmpty(true);
      }
      send.disabled = false;
      input.focus();
    },
  },
    files,
    input,
    h("div", { class: "composer-actions" },
      h("div", { class: "composer-tools" },
        h("button", { type: "button", class: "round", "aria-label": "Attach a file", title: "Attach a file (text, CSV, PDF, Word)", onclick: () => picker.click() }, mi("attach_file")),
        h("button", { type: "button", class: "round", "aria-label": "Open the live browser", title: "Live browser (sign in to shops, see what the assistant sees)", onclick: () => openBrowser("private", "") }, mi("language")),
        h("span", { class: "divider", "aria-hidden": "true" }),
        h("span", { class: "composer-hint" }, "Enter to send · Shift+Enter for a new line"),
      ),
      picker,
      send,
    ),
  );
  input.addEventListener("input", grow);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });

  const ideas = [
    ...state.roles.filter((role) => IDEAS[role]).map((role) => IDEAS[role]),
    ...PERSONAL_IDEAS,
  ].slice(0, 4);

  const status = h("span", { class: "chip-status" }, h("span", { class: "status-dot" }), "Checking the assistant...");
  serverStatus().then(({ up, model }) => fill(status,
    h("span", { class: `status-dot ${up && model ? "up" : "down"}` }),
    up && model ? "Assistant online" : up ? "Assistant starting" : "Server unavailable",
  ));

  const content = h("div", { class: "chat is-empty" },
    h("h1", { class: "sr-only" }, "Chat"),
    h("div", { class: "chat-status column" }, status),
    h("div", { class: "chat-body" },
      h("div", { class: "greeting column" },
        h("div", { class: "spark-box", "aria-hidden": "true" }, icon("spark")),
        h("h2", { class: "greeting-title" }, "How can I assist today, ", h("em", null, state.username), "?"),
        h("p", { class: "greeting-sub" }, h("span", null, "Private by default"), h("span", { class: "sep" }), h("span", null, "Work goes to the right team")),
      ),
      scroll,
      h("div", { class: "dock column" },
        error,
        composer,
        h("div", { class: "ideas" }, ideas.map(([text, iconName]) => h("button", {
          type: "button",
          onclick: () => {
            input.value = text;
            grow();
            input.focus();
          },
        }, mi(iconName), h("span", null, text), mi("north_east")))),
      ),
    ),
    h("p", { class: "fineprint column" }, mi("shield"), "Private chats are encrypted with your key. Work is seen only by the team that handles it."),
  );

  if (known()) {
    const history = await api(`/v1/chat/threads/${encodeURIComponent(thread)}/messages`);
    history.forEach((m) => bubble(m.role, m.content, m.handled_by, m.dashboards));
    if (history.length) setEmpty(false);
    const pending = await api(`/v1/chat/threads/${encodeURIComponent(thread)}/pending`).catch(() => null);
    if (pending) confirmCard(pending);
    await watchRequests();
  }
  await paintFiles();
  setTimeout(() => { input.focus(); toBottom(); }, 0);
  return content;
}


// ---- the live browser: the person takes over for sign-in, OTPs and payment ----
// The page is drawn on a canvas from screenshots (the CSP allows no other image sources). What
// the person types goes straight to the page; the agent never sees it.
function browserPanel(scope, reason, { onDone } = {}) {
  const VIEW = { w: 1280, h: 900 };
  const canvas = h("canvas", { width: VIEW.w, height: VIEW.h, class: "live-screen", tabIndex: 0, "aria-label": "Live browser view: click to interact" });
  const ctx = canvas.getContext("2d");
  const url = h("input", { placeholder: "Open a site, e.g. amazon.in", "aria-label": "Address" });
  const typed = h("input", { type: "password", autocomplete: "off", "aria-label": "Text to type into the page", placeholder: "Type here, then Send (goes to the selected field)" });
  const reveal = h("input", { type: "checkbox", onchange: () => { typed.type = reveal.checked ? "text" : "password"; } });
  const status = h("span", { class: "small muted live-url" }, "Loading...");
  const error = errorBox();
  let timer = null;
  let busy = false;
  const base = `/v1/browser/${encodeURIComponent(scope)}`;

  const draw = async () => {
    if (busy || !panel.isConnected) return;
    busy = true;
    try {
      const blob = await api(`${base}/screen`, { raw: true });
      const bitmap = await createImageBitmap(blob);
      ctx.drawImage(bitmap, 0, 0, VIEW.w, VIEW.h);
      const info = await api(base);
      status.textContent = info.open ? `${info.title || ""} — ${info.url}` : "No page open";
    } catch (err) {
      status.textContent = /no browser/i.test(err.message) ? "No page open yet: enter an address above." : err.message;
    }
    busy = false;
  };
  const act = async (body) => {
    error.clear();
    try {
      await api(`${base}/act`, { method: "POST", body });
    } catch (err) {
      error.fail(err);
    }
    setTimeout(draw, 300);
  };
  canvas.addEventListener("click", (event) => {
    const box = canvas.getBoundingClientRect();
    act({ action: "click", x: Math.round(((event.clientX - box.left) / box.width) * VIEW.w), y: Math.round(((event.clientY - box.top) / box.height) * VIEW.h) });
  });
  canvas.addEventListener("wheel", (event) => { event.preventDefault(); act({ action: "scroll", y: Math.round(event.deltaY) }); }, { passive: false });
  const sendText = () => { if (typed.value) { act({ action: "type", text: typed.value }); typed.value = ""; } };
  typed.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); sendText(); } });
  url.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); act({ action: "open", url: url.value }); } });
  const close = async (forget) => {
    clearInterval(timer);
    try { await api(`${base}/close`, { method: "POST", body: { forget } }); } catch { /* already closed */ }
    panel.remove();
  };

  const panel = h("div", { class: "card live" },
    h("div", { class: "row spread" },
      h("strong", null, icon("globe"), " Live browser", scope !== "private" ? ` (${departmentName(scope)})` : ""),
      h("div", { class: "row" },
        h("button", { type: "button", onclick: () => act({ action: "back" }) }, "Back"),
        h("button", { type: "button", onclick: () => close(false) }, "Close"),
        h("button", { type: "button", class: "danger", title: "Close and forget saved sign-ins", onclick: () => close(true) }, "Sign out & close"),
      ),
    ),
    reason && h("div", { class: "notice" }, reason),
    h("div", { class: "row" }, url, h("button", { type: "button", onclick: () => act({ action: "open", url: url.value }) }, "Go")),
    status,
    canvas,
    error,
    h("div", { class: "row wrap" },
      h("label", { class: "grow" }, "Type into the page", typed),
      h("button", { type: "button", class: "primary", onclick: sendText }, "Send"),
      ["Enter", "Tab", "Backspace"].map((k) => h("button", { type: "button", onclick: () => act({ action: "key", text: k }) }, k)),
    ),
    h("label", { class: "check small" }, reveal, h("span", null, "Show what I type")),
    h("p", { class: "small muted" }, "Click the page to select a field, then type. Passwords, OTPs and card details you type go only to "
      + "the site: the assistant never sees them. Your sign-ins are kept"
      + (scope === "private" ? ", encrypted with your key," : "") + " so you don't have to sign in every time."),
    onDone && h("button", { type: "button", class: "primary", onclick: () => onDone() }, "Done, carry on"),
  );
  timer = setInterval(() => { if (!panel.isConnected) clearInterval(timer); else if (!document.hidden) draw(); }, 1500);
  setTimeout(draw, 50);
  return panel;
}

// ---- dashboards the assistant makes (app/dashboards.py) ----
// Specs are data (they may quote web pages or mail), so every element is built here; nothing
// from a spec becomes markup. Colours are the categorical slots --cat-1..8 (style.css), given
// to series in a fixed order; values and labels stay in text colours.
const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== undefined && v !== null) el.setAttribute(k, v);
  for (const child of children.flat()) if (child) el.append(child.nodeType ? child : document.createTextNode(String(child)));
  return el;
}

const NUMBER = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const COMPACT = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const fmt = (value, unit) => (value === null || value === undefined ? "–" : NUMBER.format(value) + (unit ? ` ${unit}` : ""));

function niceScale(lo, hi, ticks = 4) {
  lo = Math.min(0, lo);
  hi = Math.max(0, hi);
  if (lo === hi) hi = lo + 1;
  const raw = (hi - lo) / ticks;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((m) => m >= raw);
  const min = Math.floor(lo / step) * step;
  const max = Math.ceil(hi / step) * step;
  const values = [];
  for (let v = min; v <= max + step / 2; v += step) values.push(Number(v.toPrecision(12)));
  return { min, max, values };
}

function seriesValues(chart) {
  return chart.series.flatMap((s) => s.values).filter((v) => v !== null && v !== undefined);
}

function legend(chart) {
  if (chart.series.length < 2) return null;
  return h("div", { class: "dash-legend" }, chart.series.map((s, i) =>
    h("span", null, h("i", { class: `swatch cat-${i + 1}`, "aria-hidden": "true" }), s.name)));
}

function chartTable(chart) {
  const total = chart.pie ? chart.series[0].values.reduce((a, b) => a + (b || 0), 0) : 0;
  return h("details", { class: "dash-data" }, h("summary", { class: "small" }, "Show the numbers"),
    table(["", ...chart.series.map((s) => s.name), ...(chart.pie ? ["Share"] : [])],
      chart.labels.map((label, i) => [label, ...chart.series.map((s) => s.values[i] ?? null),
        ...(chart.pie ? [`${total ? ((chart.series[0].values[i] || 0) / total * 100).toFixed(1) : 0}%`] : [])]
        .map((v) => (typeof v === "number" ? v : v ?? "–"))), "No data."));
}

function barChart(chart, W) {
  const H = 260; const L = 52; const R = 12; const T = 12; const B = 46;
  const scale = niceScale(Math.min(...seriesValues(chart)), Math.max(...seriesValues(chart)));
  const y = (v) => T + (H - T - B) * (1 - (v - scale.min) / (scale.max - scale.min));
  const n = chart.labels.length; const k = chart.series.length;
  const band = (W - L - R) / n;
  const groupW = Math.min(band * 0.72, 22 * k + 2 * (k - 1));
  const barW = (groupW - 2 * (k - 1)) / k;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "dash-svg", role: "img", "aria-label": chart.title || "Bar chart" });
  for (const v of scale.values) {
    root.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: v === 0 ? "axis" : "grid" }));
    root.append(svg("text", { x: L - 8, y: y(v) + 4, class: "tick", "text-anchor": "end" }, COMPACT.format(v)));
  }
  const every = Math.ceil(n / Math.max(2, Math.floor((W - L) / 56)));
  chart.labels.forEach((label, i) => {
    const x0 = L + band * i + (band - groupW) / 2;
    if (i % every === 0) {
      root.append(svg("text", { x: L + band * i + band / 2, y: H - B + 18, class: "tick", "text-anchor": "middle" },
        label.length > 12 ? `${label.slice(0, 11)}…` : label));
    }
    chart.series.forEach((s, j) => {
      const v = s.values[i];
      if (v === null || v === undefined) return;
      const x = x0 + j * (barW + 2);
      const top = y(Math.max(v, 0)); const bottom = y(Math.min(v, 0));
      const hgt = Math.max(1, bottom - top); const r = Math.min(4, barW / 2, hgt);
      // Rounded at the data end only, square on the baseline.
      const d = v >= 0
        ? `M${x},${bottom}V${top + r}Q${x},${top} ${x + r},${top}H${x + barW - r}Q${x + barW},${top} ${x + barW},${top + r}V${bottom}Z`
        : `M${x},${top}V${bottom - r}Q${x},${bottom} ${x + r},${bottom}H${x + barW - r}Q${x + barW},${bottom} ${x + barW},${bottom - r}V${top}Z`;
      const mark = svg("path", { d, class: `mark cat-${j + 1}`, tabindex: 0 });
      bindTooltip(mark, `${label}${k > 1 ? ` · ${s.name}` : ""}: ${fmt(v, chart.unit)}`);
      root.append(mark);
    });
  });
  return root;
}

function hbarChart(chart, W) {
  const s0 = chart.series;
  let order = chart.labels.map((_, i) => i);
  if (chart.pie || s0.length === 1) order.sort((a, b) => (s0[0].values[b] ?? -Infinity) - (s0[0].values[a] ?? -Infinity));
  const total = chart.pie ? s0[0].values.reduce((a, b) => a + (b || 0), 0) : 0;
  const k = s0.length; const rowH = 10 * k + 2 * (k - 1) + 14;
  const L = Math.min(150, Math.round(W * 0.32)); const R = 44; const H = order.length * rowH + 8;
  const scale = niceScale(Math.min(...seriesValues(chart)), Math.max(...seriesValues(chart)));
  const x = (v) => L + (W - L - R) * ((v - scale.min) / (scale.max - scale.min));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "dash-svg", role: "img", "aria-label": chart.title || "Bar chart" });
  root.append(svg("line", { x1: x(0), x2: x(0), y1: 0, y2: H - 4, class: "axis" }));
  order.forEach((i, row) => {
    const label = chart.labels[i];
    const top = 4 + row * rowH;
    root.append(svg("text", { x: L - 10, y: top + (rowH - 14) / 2 + 5, class: "tick", "text-anchor": "end" },
      label.length > L / 7 ? `${label.slice(0, Math.floor(L / 7) - 1)}…` : label));
    s0.forEach((s, j) => {
      const v = s.values[i];
      if (v === null || v === undefined) return;
      const y0 = top + j * 12; const a = x(Math.min(v, 0)); const b = x(Math.max(v, 0));
      const w = Math.max(1, b - a); const r = Math.min(4, w, 5);
      const d = v >= 0
        ? `M${a},${y0}H${b - r}Q${b},${y0} ${b},${y0 + r}V${y0 + 10 - r}Q${b},${y0 + 10} ${b - r},${y0 + 10}H${a}Z`
        : `M${b},${y0}H${a + r}Q${a},${y0} ${a},${y0 + r}V${y0 + 10 - r}Q${a},${y0 + 10} ${a + r},${y0 + 10}H${b}Z`;
      const share = chart.pie && total ? ` (${(v / total * 100).toFixed(1)}%)` : "";
      const mark = svg("path", { d, class: `mark cat-${j + 1}`, tabindex: 0 });
      bindTooltip(mark, `${label}${k > 1 ? ` · ${s.name}` : ""}: ${fmt(v, chart.unit)}${share}`);
      root.append(mark);
      if (k === 1 && order.length <= 15) {
        root.append(svg("text", { x: b + 6, y: y0 + 9, class: "value" }, chart.pie && total ? `${(v / total * 100).toFixed(0)}%` : COMPACT.format(v)));
      }
    });
  });
  return root;
}

function lineChart(chart, W) {
  const H = 260; const L = 52; const R = 16; const T = 12; const B = 46;
  const scale = niceScale(Math.min(...seriesValues(chart)), Math.max(...seriesValues(chart)));
  const n = chart.labels.length;
  const x = (i) => L + (n === 1 ? (W - L - R) / 2 : (W - L - R) * (i / (n - 1)));
  const y = (v) => T + (H - T - B) * (1 - (v - scale.min) / (scale.max - scale.min));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "dash-svg", role: "img", "aria-label": chart.title || "Line chart" });
  for (const v of scale.values) {
    root.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: v === 0 ? "axis" : "grid" }));
    root.append(svg("text", { x: L - 8, y: y(v) + 4, class: "tick", "text-anchor": "end" }, COMPACT.format(v)));
  }
  const every = Math.ceil(n / Math.max(2, Math.floor((W - L) / 70)));
  chart.labels.forEach((label, i) => {
    if (i % every === 0 ? n - 1 - i >= every / 2 || i === n - 1 : i === n - 1) {
      root.append(svg("text", { x: x(i), y: H - B + 18, class: "tick", "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle" },
        label.length > 12 ? `${label.slice(0, 11)}…` : label));
    }
  });
  chart.series.forEach((s, j) => {
    let d = ""; let pen = false;
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i)},${y(v)}`; pen = true;
    });
    root.append(svg("path", { d, class: `line cat-${j + 1}` }));
    if (n <= 24) s.values.forEach((v, i) => { if (v !== null && v !== undefined) root.append(svg("circle", { cx: x(i), cy: y(v), r: 4, class: `dot cat-${j + 1}` })); });
  });
  // Crosshair: one hover column per label, showing every series at that point.
  const cross = svg("line", { y1: T, y2: H - B, class: "cross", visibility: "hidden" });
  root.append(cross);
  const colW = n === 1 ? W - L - R : (W - L - R) / (n - 1);
  chart.labels.forEach((label, i) => {
    const hit = svg("rect", { x: x(i) - colW / 2, y: T, width: colW, height: H - T - B, class: "hit", tabindex: 0 });
    bindTooltip(hit, `${label}: ${chart.series.map((s) => `${chart.series.length > 1 ? `${s.name} ` : ""}${fmt(s.values[i], chart.unit)}`).join(" · ")}`);
    const on = () => { cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible"); };
    hit.addEventListener("pointerenter", on);
    hit.addEventListener("focus", on);
    hit.addEventListener("pointerleave", () => cross.setAttribute("visibility", "hidden"));
    hit.addEventListener("blur", () => cross.setAttribute("visibility", "hidden"));
    root.append(hit);
  });
  return root;
}

function csvOf(columns, rows) {
  const cell = (v) => { const t = v === null || v === undefined ? "" : String(v); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
  return [columns, ...rows].map((r) => r.map(cell).join(",")).join("\n");
}

function download(name, text, type) {
  const link = h("a", { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function renderDashboard(dash) {
  const slug = (dash.title || "dashboard").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "dashboard";
  const kpis = (dash.kpis || []).length && h("div", { class: "dash-kpis" }, dash.kpis.map((k) => h("div", { class: "dash-kpi" },
    h("div", { class: "label" }, k.label),
    h("div", { class: "value" }, k.value),
    k.change && h("div", { class: `change ${k.trend}` },
      k.trend === "up" ? "▲ " : k.trend === "down" ? "▼ " : k.trend === "flat" ? "▶ " : "", k.change),
  )));
  const draw = { bar: barChart, hbar: hbarChart, line: lineChart };
  // Drawn at the width they get (so text stays 11px), and again when that changes.
  const charts = (dash.charts || []).map((c) => {
    const plot = h("div", { class: "dash-plot" });
    let drawn = 0;
    new ResizeObserver(() => {
      const width = Math.max(280, Math.round(plot.clientWidth / 20) * 20);
      if (!plot.clientWidth || width === drawn) return;
      drawn = width;
      plot.replaceChildren(draw[c.type](c, width));
    }).observe(plot);
    return h("figure", { class: "dash-chart" },
      h("figcaption", null, c.title || "", c.unit && h("span", { class: "muted small" }, ` (${c.unit})`)),
      legend(c), plot, chartTable(c));
  });
  const tables = (dash.tables || []).map((t) => h("div", { class: "dash-table" },
    h("div", { class: "row spread" },
      h("strong", null, t.title || "Table"),
      h("button", { type: "button", class: "small", onclick: () => download(`${slug}.csv`, csvOf(t.columns, t.rows), "text/csv") }, "Download CSV"),
    ),
    table(t.columns, t.rows, "No rows."),
    t.more > 0 && h("p", { class: "small muted" }, `${t.more} more rows not shown.`),
  ));
  return h("section", { class: "dash", "aria-label": `Dashboard: ${dash.title}` },
    h("header", { class: "row spread" },
      h("div", null, h("h3", null, icon("spark"), " ", dash.title), dash.subtitle && h("p", { class: "small muted" }, dash.subtitle)),
      h("button", { type: "button", class: "small", title: "Save the dashboard's data",
        onclick: () => download(`${slug}.json`, JSON.stringify(dash, null, 2), "application/json") }, "Download"),
    ),
    kpis,
    charts.length && h("div", { class: "dash-charts" }, charts),
    tables,
    dash.notes && mdBlock(dash.notes, "dash-notes"),
  );
}

// ---- overview ----
const DEPT_ICONS = {
  account: "receipt_long", logistics: "local_shipping", researcher: "science", hr: "group",
  superuser: "inventory_2", admin: "shield", none: "block",
};
const deptLabel = (name) => (name === "none" ? "Unrouted" : departmentName(name));

// A KPI tile: caps label (and a chip or icon) on top, a serif number with its unit, then an
// optional meter and a footer line [left, right]. With a target it opens that page.
function tile(label, value, hint, target, { chip, unit, iconName, meter, foot, mono } = {}) {
  let meterEl = null;
  if (meter !== undefined && meter !== null) {
    const fillBar = h("i");
    fillBar.style.width = `${Math.max(0, Math.min(1, meter)) * 100}%`;
    meterEl = h("div", { class: "meter" }, fillBar);
  }
  const body = [
    h("div", { class: "tile-top" }, h("span", { class: `label ${mono ? "mono" : ""}` }, label), chip || (iconName && mi(iconName))),
    h("div", { class: "value" }, String(value), unit && h("span", { class: `unit ${mono ? "mono" : ""}` }, unit)),
    meterEl,
    (foot || hint) && h("div", { class: "hint" }, foot ? [h("span", null, foot[0]), foot[1] && h("strong", null, foot[1])] : h("span", null, hint)),
  ];
  return target
    ? h("button", { class: "tile", onclick: () => state.go(target) }, body)
    : h("div", { class: "tile" }, body);
}

function bindTooltip(el, text) {
  const tip = document.getElementById("tooltip");
  const place = (event) => {
    tip.textContent = text;
    tip.hidden = false;
    const box = el.getBoundingClientRect();
    tip.style.left = `${Math.min((event.clientX || box.left) + 12, window.innerWidth - 270)}px`;
    tip.style.top = `${(event.clientY || box.top) + 14}px`;
  };
  const hide = () => { tip.hidden = true; };
  el.addEventListener("pointermove", place);
  el.addEventListener("pointerleave", hide);
  el.addEventListener("focus", place);
  el.addEventListener("blur", hide);
}

function departmentTotals(activity) {
  const totals = new Map();
  for (const { department, count } of activity) totals.set(department, (totals.get(department) || 0) + count);
  return [...totals].sort((a, b) => b[1] - a[1]);
}

function activityChart(activity, rows) {
  const max = Math.max(...rows.map(([, count]) => count));
  return h("div", { class: "bars" }, rows.map(([department, count], i) => {
    const parts = activity
      .filter((a) => a.department === department)
      .map((a) => `${a.count} ${(OUTCOMES[a.outcome] || [a.outcome])[0].toLowerCase()}`)
      .join(", ");
    const bar = h("div", { class: `bar b${Math.min(i + 1, 4)}` });
    bar.style.width = `${(count / max) * 100}%`;
    const row = h("div", { class: "bar-row", tabIndex: 0, "aria-label": `${deptLabel(department)}: ${count} requests, ${parts}` },
      h("div", { class: "name" }, mi(DEPT_ICONS[department] || "group"), deptLabel(department)),
      h("div", { class: "bar-track" }, bar),
      h("div", { class: "count" }, count),
    );
    bindTooltip(row, `${deptLabel(department)}: ${parts}`);
    return row;
  }));
}

function statusChip(count, { zero, some }) {
  return count > 0 ? h("span", { class: "pill warn dot" }, some) : h("span", { class: "pill good dot" }, zero);
}

async function overviewView(refresh) {
  const data = await refreshCounts();
  const total = data.activity.reduce((sum, a) => sum + a.count, 0);
  const done = data.activity.filter((a) => a.outcome === "ok").reduce((sum, a) => sum + a.count, 0);
  const rows = departmentTotals(data.activity);
  const pct = (n) => `${total ? Math.round((n / total) * 1000) / 10 : 0}%`;
  const tiles = [
    tile(has("admin") ? "Work requests" : "Your work requests", total, null, null, {
      unit: "last 24h", chip: h("span", { class: "pill mono" }, `${rows.length} dept${rows.length === 1 ? "" : "s"}`),
      foot: ["Completed", total ? pct(done) : "–"],
    }),
    data.pending_approvals !== undefined && tile("Pending approvals", data.pending_approvals, null, "approvals", {
      unit: "waiting", chip: statusChip(data.pending_approvals, { zero: "Clear", some: "Needs you" }),
      foot: ["Paused actions", "Open →"],
    }),
    data.inbox_queued !== undefined && tile("Accounts inbox", data.inbox_queued, null, "inbox", {
      unit: "queued", chip: statusChip(data.inbox_queued, { zero: "Empty", some: "Queued" }),
      foot: ["Orders, bills, bookings", "Open →"],
    }),
    data.open_reports !== undefined && tile("Open reports", data.open_reports, null, "reports", {
      unit: "open", chip: statusChip(data.open_reports, { zero: "None", some: "Review" }),
      foot: ["Flagged content", "Open →"],
    }),
  ];
  const legend = rows.length > 0 && h("div", { class: "legend" },
    rows.slice(0, 2).map(([d, n], i) => h("span", null, h("i", { class: `b${i + 1}` }), `${deptLabel(d)} (${pct(n)})`)),
    rows.length > 2 && h("span", null, h("i", { class: "b4" }), `Others (${pct(rows.slice(2).reduce((a, [, n]) => a + n, 0))})`),
  );
  const outcomeRows = data.activity
    .slice()
    .sort((a, b) => b.count - a.count || a.department.localeCompare(b.department))
    .map((a) => {
      const [text, kind] = OUTCOMES[a.outcome] || [a.outcome, ""];
      return [h("span", { class: "dot-label" }, deptLabel(a.department)), h("span", { class: `pill dot ${kind}` }, text), a.count];
    });

  const side = data.departments
    ? h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", null, mi("shield"), "Departments"), h("span", { class: "pill mono" }, `${data.users ?? "–"} people`)),
      h("div", { class: "list-cards" }, data.departments.map((d) => h("div", { class: "list-card" },
        h("span", { class: "avatar", "aria-hidden": "true" }, initials(departmentName(d.name))),
        h("div", { class: "grow" },
          h("div", null, departmentName(d.name)),
          h("div", null, `${d.quota_per_min} requests/min · ${d.tools.includes("*") ? "all tools" : `${d.tools.length} tools`}`)),
      ))),
    )
    : h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", null, mi("person"), "Your departments")),
      h("div", { class: "list-cards" }, (state.roles.length ? state.roles : ["none"]).map((r) => h("div", { class: "list-card" },
        h("span", { class: "avatar", "aria-hidden": "true" }, initials(deptLabel(r))),
        h("div", { class: "grow" }, h("div", null, r === "none" ? "No department yet" : departmentName(r)),
          h("div", null, r === "none" ? "Ask your administrator to add you to one" : "Work you ask for here is handled by this team")),
      ))),
    );

  return h("div", null,
    heading("Overview", "Work requests handled by departments. Private conversations are never counted or logged here.", {
      eyebrow: ["Workspace", "Last 24 hours"],
      actions: h("button", { type: "button", onclick: refresh }, "Refresh"),
    }),
    h("div", { class: "tiles" }, tiles),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", null, mi("bar_chart"), "Work requests by department"), legend),
      total ? activityChart(data.activity, rows) : h("div", { class: "empty" }, "No work requests in the last 24 hours."),
      total > 0 && h("div", { class: "card-foot" },
        h("span", null, `${total} request${total === 1 ? "" : "s"} across ${rows.length} department${rows.length === 1 ? "" : "s"}`),
        h("span", null, "Hover a row for its outcomes")),
    ),
    h("div", { class: "overview-grid" },
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", null, mi("task_alt"), "Outcomes"), h("span", { class: "eyebrow" }, "Last 24 hours")),
        total ? table(["Department", "Outcome", "Requests"], outcomeRows) : h("div", { class: "empty" }, "Nothing yet."),
      ),
      side,
    ),
  );
}

// ---- approvals ----
function resultNotice(result) {
  const text = result.outcome === "pending_approval"
    ? "The agent went on to another action that needs its own approval."
    : result.reply || result.reason || "Done.";
  return h("div", { class: `notice ${result.outcome === "error" ? "error" : "good"}` }, mdBlock(text));
}

async function approvalsView(refresh) {
  const rows = await api("/v1/approvals");
  const error = errorBox();
  const decide = (id, verb) => async () => {
    error.clear();
    try {
      const result = await api(`/v1/approvals/${id}/${verb}`, { method: "POST" });
      const content = await approvalsView(refresh);
      content.prepend(resultNotice(result));
      document.querySelector(".page").replaceChildren(content);
      refreshCounts();
    } catch (err) {
      error.fail(err);
    }
  };
  return h("div", null,
    heading("Approvals", "These actions are paused until you decide. Accounts is notified of each decision on a finance action.", { eyebrow: ["Workspace", "Approvals"] }),
    error,
    h("div", { class: "card" }, table(
      ["Action", "Details", "Requested by", "Original message", "Decision"],
      rows.map((a) => [
        h("strong", null, a.tool),
        h("div", { class: "mono" }, JSON.stringify(a.tool_args)),
        a.subject_name,
        h("div", { class: "pre" }, a.message),
        h("div", { class: "row" },
          h("button", { class: "primary", onclick: decide(a.id, "approve") }, "Approve"),
          h("button", { class: "danger", onclick: decide(a.id, "deny") }, "Deny"),
        ),
      ]),
      "Nothing is waiting for approval.",
    )),
  );
}

// ---- inbox ----
async function inboxView(refresh) {
  const rows = await api("/v1/inbox?department=account");
  const error = errorBox();
  const processTask = (id) => async (event) => {
    event.target.disabled = true;
    error.clear();
    try {
      const result = await api(`/v1/inbox/${id}/process`, { method: "POST" });
      const content = await inboxView(refresh);
      content.prepend(resultNotice(result));
      document.querySelector(".page").replaceChildren(content);
      refreshCounts();
    } catch (err) {
      error.fail(err);
      event.target.disabled = false;
    }
  };
  return h("div", null,
    heading("Accounts inbox", "Orders, bookings and bills sent to accounts from chats.", { eyebrow: ["Workspace", "Accounts inbox"] }),
    error,
    h("div", { class: "card" }, table(
      ["Type", "Requested by", "Message", ""],
      rows.map((t) => [
        h("span", { class: "pill" }, t.kind),
        t.requester_name,
        h("div", { class: "pre" }, t.message),
        h("button", { class: "primary", onclick: processTask(t.id) }, "Process"),
      ]),
      "The inbox is empty.",
    )),
  );
}

// ---- reports ----
async function reportsView() {
  const rows = await api("/v1/reports");
  const KINDS = { nsfw: ["Explicit or illegal", "bad"], finance_notice: ["Admin finance activity", "warn"] };
  return h("div", null,
    heading("Reports", "Only what your role is notified about.", { eyebrow: ["Workspace", "Reports"] }),
    h("div", { class: "card" }, table(
      ["Type", "User", "Content"],
      rows.map((r) => {
        const [label, kind] = KINDS[r.kind] || [r.kind, ""];
        return [h("span", { class: `pill ${kind}` }, label), r.subject_name, h("div", { class: "pre" }, r.message)];
      }),
      "There are no open reports.",
    )),
  );
}

// ---- users ----
function departmentBoxes(selected, onChange) {  // checkbox chips
  const boxes = DEPARTMENTS.map((name) => {
    const box = h("input", { type: "checkbox", value: name, checked: selected.includes(name), onchange: onChange });
    return [box, h("label", { class: "check" }, box, h("span", null, departmentName(name)))];
  });
  return {
    node: h("div", { class: "dept-boxes" }, boxes.map(([, label]) => label)),
    value: () => boxes.filter(([box]) => box.checked).map(([box]) => box.value),
  };
}

// An inline "are you sure" step: the viewer has no dialogs, and a second click is clearer anyway.
function confirmPanel(text, actionLabel, onConfirm, { danger = false } = {}) {
  const error = errorBox();
  const go = h("button", { class: danger ? "danger" : "primary", type: "button" }, actionLabel);
  const panel = h("div", { class: "panel", hidden: true },
    h("p", null, text), error,
    h("div", { class: "row" }, go, h("button", { type: "button", onclick: () => { panel.hidden = true; } }, "Cancel")),
  );
  go.addEventListener("click", async () => {
    error.clear();
    go.disabled = true;
    try {
      await onConfirm();
    } catch (err) {
      error.fail(err);
      go.disabled = false;
    }
  });
  return panel;
}

function userCard(user, refresh) {
  const isMe = user.username === state.username;
  const isAdmin = user.roles.includes("admin");
  const error = errorBox();
  const save = h("button", { disabled: true }, "Save departments");
  const boxes = departmentBoxes(user.roles, () => { save.disabled = false; save.textContent = "Save departments"; });
  save.addEventListener("click", async () => {
    error.clear();
    save.disabled = true;
    try {
      await api(`/v1/auth/users/${user.id}/departments`, { method: "PUT", body: { departments: boxes.value() } });
      save.textContent = "Saved";
    } catch (err) {
      error.fail(err);
      save.disabled = false;
    }
  });

  const temporary = h("input", { type: "password", minLength: 12, autocomplete: "new-password", "aria-label": "Temporary password" });
  const resetError = errorBox();
  const reset = h("form", {
    class: "panel",
    hidden: true,
    onsubmit: async (event) => {
      event.preventDefault();
      resetError.clear();
      try {
        await api(`/v1/auth/users/${user.id}/reset-password`, { method: "POST", body: { password: temporary.value } });
        refresh();
      } catch (err) {
        resetError.fail(err);
      }
    },
  },
    h("p", null, `Only for someone who lost both their password and their recovery code. If ${user.username} still has `
      + "their recovery code, they should use \"Forgot password?\" on the sign-in page instead: that keeps their chats."),
    h("p", { class: "small muted" }, "With a temporary password they choose a new one at sign-in. Their earlier "
      + "private chats can't be opened again, by anyone. You still can't read their chats."),
    resetError,
    h("label", null, "Temporary password (at least 12 characters)", temporary),
    h("div", { class: "row" },
      h("button", { class: "primary", type: "submit" }, "Set temporary password"),
      h("button", { type: "button", onclick: () => { reset.hidden = true; } }, "Cancel"),
    ),
  );

  const toggle = confirmPanel(
    user.disabled
      ? `Let ${user.username} sign in again?`
      : `Disable ${user.username}? They're signed out at once and can't sign in. Nothing is deleted, and you can enable them again.`,
    user.disabled ? "Enable" : "Disable",
    async () => {
      await api(`/v1/auth/users/${user.id}/disabled`, { method: "PUT", body: { disabled: !user.disabled } });
      refresh();
    },
    { danger: !user.disabled },
  );

  const eraseName = h("input", { autocomplete: "off", "aria-label": "Type the username to confirm" });
  const eraseAccount = h("input", { type: "checkbox" });
  const eraseError = errorBox();
  const erase = h("form", {
    class: "panel",
    hidden: true,
    onsubmit: async (event) => {
      event.preventDefault();
      eraseError.clear();
      try {
        await api(`/v1/auth/users/${user.id}/erase`, {
          method: "POST", body: { confirm_username: eraseName.value, delete_account: eraseAccount.checked },
        });
        refresh();
      } catch (err) {
        eraseError.fail(err);
      }
    },
  },
    h("p", null, `Permanently delete everything stored for ${user.username}: private chats, attachments, files, `
      + "reminders, notifications, mailbox connection, and the work requests they made. This can't be undone."),
    h("p", { class: "small muted" }, "Kept: reports of explicit or illegal content, and the access log (which holds no message text). "
      + "They will need to set a new password to use private chat again."),
    eraseError,
    h("label", { class: "check" }, eraseAccount, h("span", null, "Also delete the account")),
    h("label", null, `Type ${user.username} to confirm`, eraseName),
    h("div", { class: "row" },
      h("button", { class: "danger", type: "submit" }, "Erase data"),
      h("button", { type: "button", onclick: () => { erase.hidden = true; } }, "Cancel"),
    ),
  );

  const pills = [
    isMe && h("span", { class: "pill you info" }, "YOU"),
    isAdmin && h("span", { class: "pill" }, "Administrator"),
    user.disabled
      ? h("span", { class: "pill bad dot" }, "Disabled")
      : h("span", { class: "pill good dot" }, "Active"),
    h("span", { class: `pill mono ${user.chat_enabled ? "" : "warn"}` }, user.chat_enabled ? "Own password set" : "Waiting for first sign-in"),
  ];

  return h("div", { class: `card user-card ${user.disabled ? "is-disabled" : ""}` },
    h("div", { class: "user-head" },
      h("span", { class: "avatar", "aria-hidden": "true" }, initials(user.username)),
      h("div", { class: "grow" },
        h("div", { class: "user-name" }, user.username, pills),
        h("div", { class: "user-meta" }, `ID: ${user.id}`),
      ),
    ),
    error,
    h("div", { class: "user-section" },
      h("div", { class: "caps" }, "Departments"),
      boxes.node,
    ),
    h("div", { class: "user-actions" },
      h("div", { class: "row" },
        !isMe && h("button", { type: "button", class: "ghost", onclick: () => { reset.hidden = false; toggle.hidden = true; temporary.focus(); } }, mi("lock"), "Reset password"),
        !isMe && h("button", {
          type: "button",
          class: user.disabled ? "ghost" : "ghost danger",
          onclick: () => { toggle.hidden = false; reset.hidden = true; },
        }, mi(user.disabled ? "check_circle" : "block"), user.disabled ? "Enable" : "Disable"),
        !isMe && h("button", {
          type: "button", class: "ghost danger", onclick: () => { erase.hidden = false; reset.hidden = true; toggle.hidden = true; eraseName.focus(); },
        }, mi("delete"), "Erase data"),
        isMe && h("span", { class: "small muted" }, "Change your own password under Settings."),
      ),
      save,
    ),
    reset,
    toggle,
    erase,
  );
}

async function usersView(refresh) {
  const users = await api("/v1/auth/users");
  const createError = errorBox();
  const username = h("input", { required: true, autocomplete: "off", placeholder: "e.g. priya.sharma", id: "new-username" });
  const password = h("input", { type: "password", required: true, minLength: 12, autocomplete: "new-password", placeholder: "At least 12 characters", id: "new-password" });
  const boxes = departmentBoxes([]);
  const list = h("div", { class: "user-list" });
  const search = h("input", { type: "search", placeholder: "Search by name or department...", "aria-label": "Search people" });
  const filter = h("select", { "aria-label": "Department" },
    h("option", { value: "" }, "Department: All"),
    [...DEPARTMENTS, "admin"].map((d) => h("option", { value: d }, departmentName(d))),
    h("option", { value: "-" }, "No department"));
  const count = h("span", { class: "eyebrow" });
  const paint = () => {
    const q = search.value.trim().toLowerCase();
    const shown = users.filter((u) => (!q || u.username.toLowerCase().includes(q)
      || u.roles.some((r) => departmentName(r).toLowerCase().includes(q)))
      && (!filter.value || (filter.value === "-" ? !u.roles.length : u.roles.includes(filter.value))));
    fill(list, shown.length ? shown.map((u) => userCard(u, refresh)) : h("div", { class: "card empty" }, "Nobody matches."));
    count.textContent = `Showing ${shown.length} of ${users.length} people`;
  };
  search.addEventListener("input", paint);
  filter.addEventListener("change", paint);
  paint();

  const active = users.filter((u) => !u.disabled).length;
  const waiting = users.filter((u) => !u.chat_enabled).length;
  const csv = () => download("users.csv", csvOf(["username", "departments", "status", "own_password_set"],
    users.map((u) => [u.username, u.roles.join(" "), u.disabled ? "disabled" : "active", u.chat_enabled ? "yes" : "no"])), "text/csv");
  return h("div", null,
    heading("Users & Access", "Add people, choose their departments and manage their sign-in. Changes apply right away.", {
      eyebrow: ["Administration", "Users"],
      chip: h("span", { class: "pill mono good dot" }, "Private chats encrypted"),
      actions: h("button", { type: "button", onclick: csv }, mi("download"), "Download CSV"),
    }),
    h("div", { class: "tiles" },
      tile("Total accounts", users.length, null, null, { mono: true, iconName: "group", meter: 1, unit: `${users.filter((u) => u.roles.includes("admin")).length} admin` }),
      tile("Active", active, null, null, { mono: true, iconName: "check_circle", meter: users.length ? active / users.length : 0, unit: "can sign in" }),
      tile("Disabled", users.length - active, null, null, { mono: true, iconName: "block", meter: users.length ? (users.length - active) / users.length : 0, unit: "signed out" }),
      tile("Awaiting first sign-in", waiting, null, null, { mono: true, iconName: "hourglass_empty", meter: users.length ? waiting / users.length : 0, unit: "first password" }),
    ),
    h("div", { class: "admin-grid" },
      h("div", null,
        h("div", { class: "toolbar" }, iconField("search", search), filter),
        list,
        h("div", { class: "list-foot" }, count),
      ),
      h("aside", null,
        h("form", {
          class: "card add-user",
          onsubmit: async (event) => {
            event.preventDefault();
            createError.clear();
            try {
              await api("/v1/auth/users", {
                method: "POST",
                body: { username: username.value, password: password.value, roles: boxes.value() },
              });
              refresh();
            } catch (err) {
              createError.fail(err);
            }
          },
        },
          h("div", { class: "card-head" }, h("h2", null, mi("person_add"), "Add a person")),
          h("p", { class: "muted small" }, "Choose a first password. At first sign-in they set their own, which creates the key "
            + "for their private chats. Administrators can't read those chats."),
          createError,
          h("label", { for: "new-username" }, h("span", { class: "caps" }, "Username"), iconField("alternate_email", username)),
          h("label", { for: "new-password" }, h("span", { class: "label-row" }, h("span", { class: "caps" }, "First password"), h("span", { class: "subtle small mono" }, "min 12 chars")),
            iconField("key", password)),
          h("div", { class: "caps" }, "Departments"),
          h("div", { class: "user-section" }, boxes.node),
          h("button", { class: "primary wide", type: "submit" }, mi("add"), "Add person"),
        ),
        h("div", { class: "card info-card" },
          h("h2", null, mi("verified_user"), "Private by design"),
          h("p", null, "Each person's private chats, files and saved details are encrypted with their own key. Administrators "
            + "manage access and departments, but can't open private chats, personal mail or saved logins."),
        ),
      ),
    ),
  );
}

// ---- credentials (vault secrets) ----
async function credentialsView(refresh) {
  const names = await api("/v1/admin/secrets");
  const error = errorBox();
  const name = h("input", {
    required: true, autocomplete: "off", pattern: "[A-Za-z0-9_./-]{1,128}", placeholder: "payment_gateway_key",
    title: "Letters, numbers, and _ . / - only",
  });
  const value = h("input", { type: "password", required: true, autocomplete: "new-password" });
  const submit = h("button", { class: "primary", type: "submit" }, "Save credential");

  const rows = names.map((secret) => {
    const remove = confirmPanel(
      `Delete "${secret}"? Tools that use it stop working until you add it again.`,
      "Delete",
      async () => {
        await api(`/v1/admin/secrets/${encodeURIComponent(secret)}`, { method: "DELETE" });
        refresh();
      },
      { danger: true },
    );
    return h("div", { class: "secret-row" },
      h("div", { class: "row spread" },
        h("div", { class: "row" }, icon("lock"), h("span", { class: "mono" }, secret)),
        h("div", { class: "row" },
          h("button", {
            type: "button",
            onclick: () => { name.value = secret; value.value = ""; value.focus(); submit.textContent = "Replace credential"; },
          }, "Replace"),
          h("button", { type: "button", class: "danger", onclick: () => { remove.hidden = false; } }, "Delete"),
        ),
      ),
      remove,
    );
  });

  return h("div", null,
    heading("Credentials", "API keys and passwords the platform's tools use, such as a payment gateway or email provider.", { eyebrow: ["Administration", "Credentials"] }),
    h("div", { class: "notice info" }, "Values are encrypted in the vault and are never shown again, not even here. "
      + "To change one, replace it."),
    h("div", { class: "admin-grid" },
      h("div", { class: "card" },
        h("h2", null, `Stored (${names.length})`),
        rows.length ? h("div", { class: "secret-list" }, rows) : h("div", { class: "empty" }, "No credentials yet."),
      ),
      h("form", {
        class: "card",
        onsubmit: async (event) => {
          event.preventDefault();
          error.clear();
          submit.disabled = true;
          try {
            await api("/v1/admin/secrets", { method: "PUT", body: { name: name.value, value: value.value } });
            refresh();
          } catch (err) {
            error.fail(err);
            submit.disabled = false;
          }
        },
      },
        h("h2", null, "Add or replace"),
        error,
        h("label", null, "Name", name),
        h("label", null, "Value", value),
        submit,
      ),
    ),
  );
}

// ---- my account ----
async function accountView() {
  const me = await api("/v1/auth/me");
  const error = errorBox();
  const note = h("div");
  const oldPassword = h("input", { type: "password", required: true, autocomplete: "current-password" });
  const next = h("input", { type: "password", required: true, minLength: 12, autocomplete: "new-password" });
  const codePassword = h("input", { type: "password", required: true, autocomplete: "current-password" });
  const codeError = errorBox();

  return h("div", null,
    heading("Settings", "Your mailbox, reminders, saved details and password.", { eyebrow: ["Personal", "Settings"] }),
    await mailSettingsCard(),
    await walletCard(),
    h("div", { class: "card" },
      h("h2", null, "Profile"),
      h("dl", { class: "facts" },
        h("dt", null, "Username"), h("dd", null, me.username),
        h("dt", null, "Role"), h("dd", null, me.is_admin ? "Administrator" : "Member"),
        h("dt", null, "Departments"),
        h("dd", { class: "row" }, me.departments.length
          ? me.departments.map((d) => h("span", { class: "pill" }, departmentName(d)))
          : "None. Ask your administrator if you need one."),
        h("dt", null, "Private chat"), h("dd", null, me.chat_enabled ? "On, encrypted with your key" : "Off until you set your own password"),
        h("dt", null, "Member since"), h("dd", null, new Date(me.created_at).toLocaleDateString()),
      ),
    ),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        note.replaceChildren();
        try {
          const result = await api("/v1/auth/change-password", {
            method: "POST", body: { old_password: oldPassword.value, new_password: next.value },
          });
          useToken(result, state.username);
          if (result.recovery_code) return recoveryCodeScreen(result.recovery_code);
          oldPassword.value = "";
          next.value = "";
          note.replaceChildren(h("div", { class: "notice good" }, "Password changed. Your recovery code still works."));
        } catch (err) {
          error.fail(err);
        }
      },
    },
      h("h2", null, "Change password"),
      error,
      note,
      h("label", null, "Current password", oldPassword),
      h("label", null, "New password (at least 12 characters)", next),
      h("button", { class: "primary", type: "submit" }, "Change password"),
    ),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        codeError.clear();
        try {
          const result = await api("/v1/auth/recovery-code", {
            method: "POST", body: { password: codePassword.value },
          });
          recoveryCodeScreen(result.recovery_code);
        } catch (err) {
          codeError.fail(err);
        }
      },
    },
      h("h2", null, "Replace recovery code"),
      h("p", { class: "muted" }, "Lost your code? This creates a new one, and the old one stops working."),
      codeError,
      h("label", null, "Current password", codePassword),
      h("button", { type: "submit" }, "Create new code"),
    ),
  );
}


// ---- saved details: logins, cards, UPI, addresses (encrypted with the person's key) ----
const WALLET_FORMS = {
  login: [["site", "Website, e.g. amazon.in"], ["username", "Username or email"], ["password", "Password", "password"], ["label", "Name (optional)"]],
  card: [["label", "Name, e.g. HDFC credit card"], ["name", "Name on card"], ["number", "Card number", "password"], ["expiry", "Expiry (MM/YY)"]],
  upi: [["upi_id", "UPI ID, e.g. name@okhdfc"], ["label", "Name (optional)"]],
  address: [["label", "Name, e.g. Home"], ["name", "Full name"], ["phone", "Phone"], ["line1", "Address line 1"], ["line2", "Address line 2"],
    ["city", "City"], ["state", "State"], ["pincode", "PIN code"]],
};

async function walletCard() {
  const card = h("div", { class: "card" });
  const items = await api("/v1/me/wallet");
  const error = errorBox();
  const kind = h("select", null, [["login", "Website login"], ["card", "Card"], ["upi", "UPI ID"], ["address", "Address"]]
    .map(([v, t]) => h("option", { value: v }, t)));
  const fields = h("div", { class: "row wrap" });
  let inputs = {};
  const drawFields = () => {
    inputs = {};
    fill(fields, WALLET_FORMS[kind.value].map(([name, label, type]) => {
      const input = h("input", { type: type || "text", autocomplete: "off" });
      inputs[name] = input;
      return h("label", null, label, input);
    }));
  };
  kind.addEventListener("change", drawFields);
  drawFields();
  const redraw = async () => card.replaceWith(await walletCard());
  const reveal = (item, out) => async () => {
    const pw = h("input", { type: "password", autocomplete: "current-password", "aria-label": "Your account password" });
    const err = errorBox();
    out.replaceChildren(h("div", { class: "row" }, pw, h("button", { type: "button", onclick: async () => {
      err.clear();
      try {
        const data = await api(`/v1/me/wallet/${item.id}/reveal`, { method: "POST", body: { password: pw.value } });
        delete data.kind;
        out.replaceChildren(h("dl", { class: "facts" }, Object.entries(data).flatMap(([k, v]) => [h("dt", null, k), h("dd", { class: "mono" }, v)])),
          h("button", { type: "button", onclick: () => out.replaceChildren() }, "Hide"));
      } catch (e) { err.fail(e); }
    } }, "Unlock"), err));
    pw.placeholder = "Your account password";
    pw.focus();
  };
  fill(card,
    h("h2", null, "Saved details"),
    h("p", { class: "muted" }, "Logins, cards, UPI IDs and addresses the assistant can use to sign in and order for you. They're "
      + "encrypted with your key: nobody else can see them, the admin included, and the assistant never sees the values; it "
      + "only fills them in on the right site. CVVs and OTPs are never saved: you're asked at each payment."),
    items.length
      ? h("div", { class: "notes" }, items.map((item) => {
        const out = h("div");
        return h("div", { class: "card note" },
          h("div", { class: "row spread" }, h("span", null, h("span", { class: "pill" }, item.kind), " ", item.summary),
            h("div", { class: "row" },
              h("button", { type: "button", onclick: reveal(item, out) }, "Show"),
              h("button", { type: "button", class: "danger", onclick: async () => { await api(`/v1/me/wallet/${item.id}`, { method: "DELETE" }); redraw(); } }, "Delete"))),
          out);
      }))
      : h("div", { class: "empty" }, "Nothing saved yet."),
    h("form", {
      class: "settings",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        const data = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
        try { await api("/v1/me/wallet", { method: "POST", body: { kind: kind.value, data } }); redraw(); } catch (e) { error.fail(e); }
      },
    }, h("h3", null, "Add"), error, h("label", null, "Kind", kind), fields, h("button", { class: "primary", type: "submit" }, "Save")),
  );
  return card;
}

// ---- notifications (the bell) ----
const KIND_LABELS = { reminder: ["Reminder", "warn"], digest: ["Mail digest", ""], alert: ["Server alert", "bad"], mail: ["Mail", ""] };

async function notificationsView(refresh) {
  const items = await api("/v1/notifications");
  const allow = "Notification" in window && Notification.permission === "default"
    && h("button", { type: "button", onclick: async () => { await Notification.requestPermission(); refresh(); } }, "Show pop-ups in this browser");
  const readAll = async () => {
    await api("/v1/notifications/read", { method: "POST", body: {} });
    state.unread = 0;
    state.counts.unread_notifications = 0;
    refresh();
  };
  const open = (n, card) => async () => {
    card.classList.toggle("open");
    if (!n.read) {
      const { unread } = await api("/v1/notifications/read", { method: "POST", body: { id: n.id } });
      state.unread = unread;
      state.counts.unread_notifications = unread;
      card.classList.remove("unread");
      n.read = true;
    }
  };
  return h("div", null,
    heading("Notifications", "Reminders, your morning mail digest and, for the admin, server alerts.", { eyebrow: ["Personal", "Notifications"] }),
    h("div", { class: "row" }, items.some((n) => !n.read) && h("button", { type: "button", onclick: readAll }, "Mark all read"), allow),
    items.length
      ? h("div", { class: "notes" }, items.map((n) => {
        const [label, kind] = KIND_LABELS[n.kind] || [n.kind, ""];
        const card = h("button", { type: "button", class: `card note ${n.read ? "" : "unread"}` },
          h("div", { class: "row spread" },
            h("span", { class: "row" }, h("span", { class: `pill ${kind}` }, label), h("strong", null, n.title)),
            h("span", { class: "small muted" }, new Date(n.created_at).toLocaleString()),
          ),
          n.body && mdBlock(n.body, "note-body"),
        );
        card.addEventListener("click", open(n, card));
        return card;
      }))
      : h("div", { class: "card empty" }, "Nothing yet. Reminders you set in chat appear here when they're due."),
  );
}

// ---- My files ----
async function filesView(refresh, { trash = false } = {}) {
  const data = await api(`/v1/files${trash ? "?trash=true" : ""}`);
  const error = errorBox();
  const folder = h("input", { placeholder: "Folder (optional), e.g. notes", "aria-label": "Folder" });
  const picker = h("input", { type: "file", class: "hidden-input", multiple: true });
  const page = h("div");
  const redraw = async (opts) => page.replaceChildren(await filesView(refresh, opts));
  picker.addEventListener("change", async () => {
    error.clear();
    for (const file of picker.files) {
      const form = new FormData();
      form.append("file", file);
      form.append("folder", folder.value);
      try {
        await api("/v1/files", { method: "POST", form });
      } catch (err) {
        error.fail(err);
      }
    }
    picker.value = "";
    if (!error.hidden) return;
    redraw({ trash });
  });
  const download = (f) => async () => {
    const blob = await api(`/v1/files/${f.id}`, { raw: true });
    const link = h("a", { href: URL.createObjectURL(blob), download: f.path.split("/").pop() });
    link.click();
    URL.revokeObjectURL(link.href);
  };
  const act = (fn) => async () => {
    error.clear();
    try { await fn(); redraw({ trash }); } catch (err) { error.fail(err); }
  };
  const preview = h("div");
  const view = (f) => async () => {
    const text = await (await api(`/v1/files/${f.id}`, { raw: true })).text();
    preview.replaceChildren(h("div", { class: "card" },
      h("div", { class: "row spread" }, h("strong", { class: "mono" }, f.path), h("button", { type: "button", onclick: () => preview.replaceChildren() }, "Close")),
      /\.md$/i.test(f.path) ? mdBlock(text) : h("pre", { class: "md-code" }, text),
    ));
    preview.scrollIntoView({ behavior: "smooth" });
  };
  page.append(
    heading(trash ? "Trash" : "My files", trash
      ? "Deleted files are kept for 7 days, then removed for good."
      : "Encrypted with your key: only you, and the assistant in your private chats, can open them. "
        + "Ask the assistant to write notes, edit lists or organize files. Every change keeps the old version.",
      { eyebrow: ["Personal", trash ? "Trash" : "My files"] }),
    error,
    h("div", { class: "row" },
      !trash && folder,
      !trash && h("button", { class: "primary", type: "button", onclick: () => picker.click() }, icon("plus"), "Upload"),
      picker,
      h("button", { type: "button", onclick: () => redraw({ trash: !trash }) }, trash ? "Back to files" : "Trash"),
      h("span", { class: "small muted" }, `${formatSize(data.used)} of ${formatSize(data.quota)} used`),
    ),
    h("div", { class: "card" }, table(
      ["Name", "Size", trash ? "Deleted" : "Changed", ""],
      data.files.map((f) => [
        h("span", { class: "mono" }, f.path),
        formatSize(f.size),
        new Date(trash ? f.deleted_at : f.updated_at).toLocaleString(),
        h("div", { class: "row" }, trash
          ? h("button", { type: "button", onclick: act(() => api(`/v1/files/${f.id}/restore`, { method: "POST" })) }, "Restore")
          : [
            /\.(md|txt|csv|json|html?)$/i.test(f.path) && h("button", { type: "button", onclick: view(f) }, "View"),
            h("button", { type: "button", onclick: download(f) }, "Download"),
            h("button", { type: "button", class: "danger", "aria-label": `Delete ${f.path}`, onclick: act(() => api(`/v1/files/${f.id}`, { method: "DELETE" })) }, "Delete"),
          ]),
      ]),
      trash ? "The trash is empty." : "No files yet. Upload some, or ask the assistant to write one.",
    )),
    preview,
  );
  return page;
}

// ---- reminders ----
async function remindersView(refresh) {
  const items = await api("/v1/reminders");
  const error = errorBox();
  const when = h("input", { required: true, placeholder: "tomorrow 9am, in 2 hours, friday 17:30", "aria-label": "When" });
  const text = h("input", { required: true, maxLength: 500, placeholder: "What to remember", "aria-label": "What" });
  const repeat = h("select", { "aria-label": "Repeat" },
    ["none", "daily", "weekdays", "weekly", "monthly"].map((r) => h("option", { value: r }, r === "none" ? "Once" : r[0].toUpperCase() + r.slice(1))));
  const email = h("input", { type: "checkbox" });
  return h("div", null,
    heading("Reminders", "You can also just ask in chat: \"remind me to call the bank tomorrow at 10\".", { eyebrow: ["Personal", "Reminders"] }),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        try {
          await api("/v1/reminders", { method: "POST", body: { when: when.value, text: text.value, repeat: repeat.value, email: email.checked } });
          refresh();
        } catch (err) {
          error.fail(err);
        }
      },
    },
      h("h2", null, "New reminder"),
      error,
      h("div", { class: "row wrap" },
        h("label", { class: "grow" }, "What", text),
        h("label", null, "When", when),
        h("label", null, "Repeat", repeat),
      ),
      h("label", { class: "check" }, email, h("span", null, "Also email it to me")),
      h("button", { class: "primary", type: "submit" }, "Add reminder"),
    ),
    h("div", { class: "card" }, table(
      ["When", "What", "Repeats", ""],
      items.map((r) => [
        r.due,
        h("span", null, r.text, r.source === "mail" && h("span", { class: "pill small" }, " from mail")),
        r.repeat === "none" ? "" : r.repeat,
        h("button", { type: "button", onclick: async () => { await api(`/v1/reminders/${r.id}`, { method: "DELETE" }); refresh(); } }, "Cancel"),
      ]),
      "No reminders waiting.",
    )),
  );
}

// ---- My account: mailbox and reminder settings ----
async function mailSettingsCard() {
  const [box, prefs] = await Promise.all([api("/v1/me/mailbox"), api("/v1/me/prefs")]);
  const card = h("div", { class: "card" });
  const error = errorBox();
  const note = h("div");
  const field = (label, props) => {
    const input = h("input", props);
    return [input, h("label", null, label, input)];
  };
  const [address, addressLabel] = field("Email address", { type: "email", required: true, autocomplete: "off", value: box.username || "" });
  const [password, passwordLabel] = field("App password", { type: "password", required: true, autocomplete: "new-password", placeholder: "16 letters for Gmail" });
  const [imapHost, imapHostLabel] = field("IMAP server", { placeholder: "imap.example.com" });
  const [smtpHost, smtpHostLabel] = field("SMTP server (to send)", { placeholder: "smtp.example.com" });
  const [smtpPort, smtpPortLabel] = field("SMTP port", { type: "number", value: 465, min: 1 });
  const other = h("details", null, h("summary", { class: "small" }, "Another provider (enter its servers)"),
    h("div", { class: "row wrap" }, imapHostLabel, smtpHostLabel, smtpPortLabel));

  const tz = h("input", { value: prefs.timezone || "", placeholder: "Asia/Kolkata" });
  const emailAddr = h("input", { type: "email", value: prefs.email || "", placeholder: "Defaults to your mailbox address" });
  const digestOn = h("input", { type: "checkbox", checked: prefs.digest_hour !== null && prefs.digest_hour !== undefined });
  const digestHour = h("select", null, Array.from({ length: 24 }, (_, i) => h("option", { value: i, selected: (prefs.digest_hour ?? 8) === i }, `${String(i).padStart(2, "0")}:00`)));
  const digestEmail = h("input", { type: "checkbox", checked: !!prefs.digest_email });
  const emailReminders = h("input", { type: "checkbox", checked: !!prefs.email_reminders });
  const prefsError = errorBox();
  const prefsNote = h("div");

  const connect = h("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      error.clear();
      note.replaceChildren(h("div", { class: "notice" }, "Checking the login..."));
      try {
        await api("/v1/me/mailbox", {
          method: "PUT",
          body: {
            address: address.value, password: password.value,
            imap_host: imapHost.value, imap_port: 993, smtp_host: smtpHost.value, smtp_port: Number(smtpPort.value) || 465,
          },
        });
        password.value = "";
        card.replaceWith(await mailSettingsCard());
      } catch (err) {
        note.replaceChildren();
        error.fail(err);
      }
    },
  },
    h("h3", null, "Or connect with an app password"),
    h("p", { class: "small muted" }, "Gmail: your normal password won't work. Turn on 2-Step Verification, then create an app password at ",
      mdLink("https://myaccount.google.com/apppasswords", "myaccount.google.com/apppasswords"),
      " (16 letters) and paste it here. Also turn on IMAP in Gmail: Settings > See all settings > Forwarding and POP/IMAP. "
      + "Outlook/Hotmail can't use a password (Microsoft blocks it): use Sign in with Microsoft above."),
    error, note,
    h("div", { class: "row wrap" }, addressLabel, passwordLabel),
    other,
    h("button", { class: "primary", type: "submit" }, box.connected ? "Update mailbox" : "Connect mailbox"),
  );

  const digestNow = h("button", { type: "button" }, "Summarize my mail now");
  digestNow.addEventListener("click", async () => {
    prefsError.clear();
    digestNow.disabled = true;
    prefsNote.replaceChildren(h("div", { class: "notice" }, "Reading your mail..."));
    try {
      const { text } = await api("/v1/me/digest", { method: "POST" });
      prefsNote.replaceChildren(h("div", { class: "notice good" }, mdBlock(text || "No new mail.")));
    } catch (err) {
      prefsNote.replaceChildren();
      prefsError.fail(err);
    }
    digestNow.disabled = false;
  });

  fill(card,
    h("h2", null, "Mail and reminders"),
    h("p", { class: "muted" }, "Connect your own mailbox so the assistant can read, summarize and answer your mail in your private chats. "
      + "Sending always asks you first."),
    box.connected && h("div", { class: "row spread" },
      h("span", null, h("span", { class: "pill good" }, "Connected"), ` ${box.username} (${box.imap_host}, ${box.method || "password"})`),
      h("button", {
        type: "button", class: "danger",
        onclick: async () => { await api("/v1/me/mailbox", { method: "DELETE" }); card.replaceWith(await mailSettingsCard()); },
      }, "Disconnect"),
    ),
    await signInBlock(box, address, async () => card.replaceWith(await mailSettingsCard())),
    connect,
    h("form", {
      class: "settings",
      onsubmit: async (event) => {
        event.preventDefault();
        prefsError.clear();
        try {
          await api("/v1/me/prefs", {
            method: "PUT",
            body: {
              timezone: tz.value.trim(), email: emailAddr.value.trim(),
              digest_hour: digestOn.checked ? Number(digestHour.value) : null,
              digest_email: digestEmail.checked, email_reminders: emailReminders.checked,
            },
          });
          prefsNote.replaceChildren(h("div", { class: "notice good" }, "Saved."));
        } catch (err) {
          prefsError.fail(err);
        }
      },
    },
      h("h3", null, "Daily digest and reminders"),
      prefsError, prefsNote,
      h("div", { class: "row" },
        h("label", { class: "check" }, digestOn, h("span", null, "Every morning, summarize my new mail and set reminders for deadlines in it, at")),
        digestHour,
      ),
      h("label", { class: "check" }, digestEmail, h("span", null, "Also email me the digest")),
      h("label", { class: "check" }, emailReminders, h("span", null, "Email me every reminder, too")),
      h("div", { class: "row wrap" },
        h("label", null, "Time zone", tz),
        h("label", { class: "grow" }, "Email address for reminders", emailAddr),
      ),
      h("div", { class: "row" },
        h("button", { class: "primary", type: "submit" }, "Save"),
        box.connected && digestNow,
      ),
    ),
  );
  return card;
}

// Sign in with Google (paste back the address the browser ends on) or Microsoft (a device code).
async function signInBlock(box, address, done) {
  const error = errorBox();
  const step = h("div");
  const ready = box.sign_in || {};
  const notSetUp = "Not set up on this server yet: ask your administrator to add it (Credentials page).";
  let polling = null;
  const stop = () => { if (polling) clearTimeout(polling); polling = null; };

  const google = h("button", { type: "button", class: "primary", disabled: !ready.google, title: ready.google ? "" : notSetUp });
  google.append("Sign in with Google");
  google.addEventListener("click", async () => {
    error.clear(); stop();
    try {
      const start = await api("/v1/me/mailbox/oauth/google/start", { method: "POST", body: { address: address.value.trim() } });
      const pasted = h("input", { required: true, placeholder: "http://127.0.0.1:8765/?state=...&code=...", "aria-label": "Address from the browser" });
      const finish = h("form", {
        class: "card sunk",
        onsubmit: async (event) => {
          event.preventDefault();
          error.clear();
          step.append(h("div", { class: "notice" }, "Checking your mailbox..."));
          try {
            await api("/v1/me/mailbox/oauth/google/finish", { method: "POST", body: { url: pasted.value } });
            await done();
          } catch (err) {
            step.lastChild?.remove();
            error.fail(err);
          }
        },
      },
        h("ol", { class: "small" },
          h("li", null, mdLink(start.url, "Open the Google sign-in"), " and choose your account. Allow access to your mail."),
          start.paste
            ? h("li", null, "Google then sends you to a page that doesn't load (\"This site can't be reached\"). That's expected: copy the whole address from the address bar and paste it here.")
            : h("li", null, "When Google says you're connected, come back here and press Done."),
        ),
        start.paste && h("label", null, "Address from the browser", pasted),
        start.paste
          ? h("button", { class: "primary", type: "submit" }, "Finish connecting")
          : h("button", { class: "primary", type: "button", onclick: done }, "Done"),
      );
      fill(step, finish);
      window.open(start.url, "_blank", "noopener,noreferrer");
    } catch (err) {
      error.fail(err);
    }
  });

  const microsoft = h("button", { type: "button", class: "primary", disabled: !ready.microsoft, title: ready.microsoft ? "" : notSetUp });
  microsoft.append("Sign in with Microsoft");
  microsoft.addEventListener("click", async () => {
    error.clear(); stop();
    try {
      const start = await api("/v1/me/mailbox/oauth/microsoft/start", { method: "POST" });
      const status = h("div", { class: "notice" }, "Waiting for you to sign in...");
      fill(step, h("div", { class: "card sunk" },
        h("ol", { class: "small" },
          h("li", null, "Open ", mdLink(start.verification_uri, start.verification_uri), "."),
          h("li", null, "Enter this code: ", h("code", { class: "big-code" }, start.user_code)),
          h("li", null, "Sign in with your Outlook, Hotmail or Microsoft 365 account and allow access to your mail."),
        ),
        status,
      ));
      const until = Date.now() + start.expires_in * 1000;
      const poll = async () => {
        if (!step.isConnected) return stop();
        if (Date.now() > until) { status.textContent = "The code expired. Press Sign in with Microsoft again."; return stop(); }
        try {
          const result = await api("/v1/me/mailbox/oauth/microsoft/poll", { method: "POST" });
          if (result.done) { stop(); await done(); return; }
          polling = setTimeout(poll, Math.max(3, start.interval) * 1000);
        } catch (err) {
          stop();
          status.remove();
          error.fail(err);
        }
      };
      polling = setTimeout(poll, Math.max(3, start.interval) * 1000);
    } catch (err) {
      error.fail(err);
    }
  });

  return h("div", { class: "signin" },
    h("h3", null, box.connected ? "Switch to signing in" : "Sign in (recommended)"),
    h("p", { class: "small muted" }, "Sign in on Google's or Microsoft's own page: no app password needed, and your password never reaches this app. "
      + "Google: Gmail and Google Workspace. Microsoft: Outlook.com, Hotmail, Live and Microsoft 365."),
    h("div", { class: "row wrap" }, google, microsoft),
    !(ready.google && ready.microsoft) && h("p", { class: "small muted" },
      !ready.google && !ready.microsoft ? notSetUp : `${!ready.google ? "Google" : "Microsoft"} sign-in: ${notSetUp}`),
    error, step,
  );
}

// ---- document templates ----
async function templatesView(refresh) {
  const rows = await api("/v1/templates");
  const error = errorBox();
  const listError = errorBox();
  const scopes = [...state.roles.filter((r) => r !== "admin"), ...(has("admin") ? [...DEPARTMENTS, "global"] : [])]
    .filter((v, i, a) => a.indexOf(v) === i);
  const name = h("input", { required: true, maxLength: 128, placeholder: "e.g. Offer letter" });
  const description = h("input", { maxLength: 512, placeholder: "What it's for (optional)" });
  const scope = h("select", null, scopes.map((sc) => h("option", { value: sc }, sc === "global" ? "Everyone (global)" : departmentName(sc))));
  const file = h("input", { type: "file", required: true, accept: ".docx,.md,.txt,.html,.csv,.json,.pdf" });
  const download = (t) => async () => {
    const blob = await api(`/v1/templates/${t.id}`, { raw: true });
    const link = h("a", { href: URL.createObjectURL(blob), download: t.filename });
    link.click();
    URL.revokeObjectURL(link.href);
  };
  return h("div", null,
    heading("Templates", "Company document templates. Ask the assistant at work, e.g. \"make offer letters for these "
      + "three people from the Offer letter template\". Your own templates go in My files, folder templates/, or attach one to a chat.",
      { eyebrow: ["Workspace", "Templates"] }),
    listError,
    h("div", { class: "card" }, table(
      ["Name", "For", "Format", "Fills in", ""],
      rows.map((t) => [
        h("span", null, h("strong", null, t.name), t.description && h("div", { class: "small muted" }, t.description)),
        t.scope === "global" ? "Everyone" : departmentName(t.scope),
        t.filename.split(".").pop().toUpperCase(),
        t.fields.length ? h("span", { class: "mono small" }, t.fields.join(", ")) : h("span", { class: "muted small" }, "Example: imitated"),
        h("div", { class: "row" },
          h("button", { type: "button", onclick: download(t) }, "Download"),
          h("button", { type: "button", class: "danger", onclick: async () => {
            listError.clear();
            try { await api(`/v1/templates/${t.id}`, { method: "DELETE" }); refresh(); } catch (err) { listError.fail(err); }
          } }, "Delete"),
        ),
      ]),
      "No templates yet.",
    )),
    scopes.length > 0 && h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        const form = new FormData();
        form.append("file", file.files[0]);
        form.append("name", name.value);
        form.append("scope", scope.value);
        form.append("description", description.value);
        try { await api("/v1/templates", { method: "POST", form }); refresh(); } catch (err) { error.fail(err); }
      },
    },
      h("h2", null, "Add a template"),
      error,
      h("div", { class: "row wrap" },
        h("label", null, "Name", name),
        h("label", null, "For", scope),
        h("label", { class: "grow" }, "Description", description),
      ),
      h("label", null, "File (Word, Markdown, text, HTML, CSV, JSON; a PDF works as an example)", file),
      h("p", { class: "small muted" }, "Mark the parts to fill with {{ name }}, {{ joining_date }} and so on. In Word, "
        + "a table row using {{ item.description }} and {{ item.amount }} repeats once per line item. A file "
        + "without placeholders works too: new documents copy its structure and style with the new facts."),
      h("button", { class: "primary", type: "submit" }, "Upload"),
    ),
  );
}

loginScreen();

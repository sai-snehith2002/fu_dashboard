WITH team AS (
  SELECT m._id, m."updatedAt",
         (SELECT t.tl__val_
          FROM lead_team_member_tl t
          WHERE t.tl__rid_ = m._id
          ORDER BY t.tl__index_
          LIMIT 1) AS tl,
         (SELECT z.zsm__val_
          FROM lead_team_member_zsm z
          WHERE z.zsm__rid_ = m._id
          ORDER BY z.zsm__index_
          LIMIT 1) AS zsm
  FROM lead_team_member m
  WHERE COALESCE(m."isDelete", 0) <> 1
),

map AS (
  SELECT sc_id, tl, zsm
  FROM (
    SELECT
        s.sc__val_ AS sc_id,
        t.tl,
        t.zsm,
        row_number() OVER (
          PARTITION BY s.sc__val_
          ORDER BY t."updatedAt" DESC NULLS LAST
        ) AS rn
    FROM lead_team_member_sc s
    JOIN team t
      ON t._id = s.sc__rid_
  ) q
  WHERE rn = 1
),

base AS (
  SELECT
         l.lead_id,
         initcap(trim(l.site_address_city)) AS city,
         l.site_address_cluster AS cluster,
         l.stage AS lighthouse_status,
         l.status AS lead_status,
         l.order_closure_datetime,
         (l.first_meeting_done_date AT TIME ZONE 'Asia/Kolkata')::date
           AS first_meeting_done_date,
         initcap(trim(concat_ws(' ', scu."firstName", scu."lastName")))
           AS sc_name,
         initcap(trim(concat_ws(' ', tlu."firstName", tlu."lastName")))
           AS tl_name,
         initcap(trim(concat_ws(' ', zu."firstName", zu."lastName")))
           AS zsm_name,
         scu.sc_type AS sc_channel,
         (scu.sc_type = 'Field Sale SC') AS is_field_sale_sc,
         (tlu._id IS NOT NULL) AS tl_resolved,
         COALESCE(
           (l.order_closure_datetime AT TIME ZONE 'Asia/Kolkata')::date =
           (l.first_meeting_done_date AT TIME ZONE 'Asia/Kolkata')::date,
           false
         ) AS is_on_spot,
         (l.status IN ('Booked','Closed - Won')) AS is_booked,
         (l.status = 'Closed - Lost') AS is_lost,
         CAST({{end_date}} AS date) AS as_of

  FROM lead l

  LEFT JOIN map m
    ON m.sc_id = l.assigned_sc

  LEFT JOIN users scu
    ON scu._id = l.assigned_sc

  LEFT JOIN users tlu
    ON tlu._id = m.tl

  LEFT JOIN users zu
    ON zu._id = m.zsm

  WHERE l.type = 'lead'
    AND COALESCE(l."isDelete", 0) <> 1

    AND l.first_meeting_done_date >= (
      CAST({{cohort_from}} AS date)::timestamp
      AT TIME ZONE 'Asia/Kolkata'
    )

    AND l.first_meeting_done_date < (
      (CAST({{end_date}} AS date) + 1)::timestamp
      AT TIME ZONE 'Asia/Kolkata'
    )

    AND lower(trim(l.site_address_city)) IN (
      'bhopal',
      'jaipur',
      'lucknow',
      'nagpur',
      'delhi',
      'ghaziabad',
      'noida',
      'gurgaon',
      'faridabad',
      'pune',
      'bangalore',
      'chennai',
      'hyderabad',
      'indore',
      'new delhi',
      'south delhi',
      'east delhi',
      'north west delhi',
      'north east delhi',
      'central delhi',
      'north delhi',
      'west delhi',
      'south west delhi',
      'gautam buddha nagar',
      'greater noida',
      'gothda mohbtabad'
    )

[[AND initcap(trim(l.site_address_city)) = {{city}}]]
[[AND initcap(trim(l.site_address_cluster)) = {{cluster}}]]
[[AND initcap(trim(concat_ws(' ', tlu."firstName", tlu."lastName"))) = {{tl}}]]
),

mtg AS (
  SELECT DISTINCT ON (h.lead_id)
         h.lead_id,
         h.meeting_schedule_date,
         h.meeting_done_date,
         h.city AS meeting_city,
         h.disposition_status,
         h.disposition_audio_path AS audio_path,
         h."disposition_audio_durationSec" AS audio_duration_sec,
         (h.disposition_submitted_at AT TIME ZONE 'Asia/Kolkata')
           AS note_submitted_at_ist,
         h.assigned_sc AS meeting_assigned_sc,
         h.assigned_tl AS meeting_assigned_tl
  FROM meeting_metrics_history h
  JOIN base b
    ON b.lead_id = h.lead_id
  WHERE h.meeting_type = 'fresh_meeting'
    AND h.meeting_done_date >= (
      CAST({{start_date}} AS date)::timestamp
      AT TIME ZONE 'Asia/Kolkata'
    )
    AND h.meeting_done_date < (
      (CAST({{end_date}} AS date) + 1)::timestamp
      AT TIME ZONE 'Asia/Kolkata'
    )
  ORDER BY
      h.lead_id,
      (h.disposition_audio_path IS NOT NULL) DESC,
      h.meeting_done_date DESC
),

mtg_counts AS (
  SELECT
      h.lead_id,
      count(*) AS meetings_in_range,
      count(*) FILTER (
        WHERE h.disposition_status = 'COMPLETED'
      ) AS notes_submitted_in_range
  FROM meeting_metrics_history h
  JOIN base b
    ON b.lead_id = h.lead_id
  WHERE h.meeting_type = 'fresh_meeting'
    AND h.meeting_done_date >= (
      CAST({{start_date}} AS date)::timestamp
      AT TIME ZONE 'Asia/Kolkata'
    )
    AND h.meeting_done_date < (
      (CAST({{end_date}} AS date) + 1)::timestamp
      AT TIME ZONE 'Asia/Kolkata'
    )
  GROUP BY 1
),

disp AS (
  SELECT DISTINCT ON (d.lead_id)
         d.lead_id,
         d._id AS disposition_id,
         d.overall_disposition,
         d.summary,
         d.meta_sales_channel,
         (d.timestamps_received_at AT TIME ZONE 'Asia/Kolkata')
           AS disposition_received_ist
  FROM dialogiq_dispositions d
  JOIN base b
    ON b.lead_id = d.lead_id
  ORDER BY
      d.lead_id,
      d.timestamps_received_at DESC NULLS LAST
),

qa AS (
  SELECT
    q.qa_pairs__rid_ AS rid,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Timeline to go Solar'
      THEN q.qa_pairs_answer
    END) AS q_timeline,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Bill Value'
      THEN q.qa_pairs_answer
    END) AS q_bill,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Decision Maker'
      THEN q.qa_pairs_answer
    END) AS q_dm,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Competitor Quote'
      THEN q.qa_pairs_answer
    END) AS q_competitor,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Main Objection'
      THEN q.qa_pairs_answer
    END) AS q_objection,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Site Visit Done?'
      THEN q.qa_pairs_answer
    END) AS q_site_visit,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Quoted Price'
      THEN q.qa_pairs_answer
    END) AS q_price,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Buffer / Margin Left'
      THEN q.qa_pairs_answer
    END) AS q_buffer,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Next Follow up'
      THEN q.qa_pairs_answer
    END) AS q_next_fu,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'SC''s Reading'
      THEN q.qa_pairs_answer
    END) AS q_reading,

    MAX(CASE
      WHEN q.qa_pairs_question_label = 'Offering'
      THEN q.qa_pairs_answer
    END) AS q_offering,

    count(*) FILTER (
      WHERE (
        q.qa_pairs_answered IS FALSE
        OR q.qa_pairs_answer IS NULL
        OR q.qa_pairs_answer = ''
      )
      -- 'Remarks' is a free-text field, not a structured disposition -- it's
      -- excluded from the CSV entirely (see the removed q_remarks column
      -- below) and from this missing-count, which now tracks the 11 named
      -- disposition questions above, not 12.
      AND q.qa_pairs_question_label <> 'Remarks'
    ) AS dispositions_missing

  FROM dialogiq_dispositions_qa_pairs q
  GROUP BY 1
),

tasks AS (
  SELECT
      t.lead_id,
      t.status,
      t."sourceOutcome" AS outcome,
      t."ownerRole" AS owner_role,
      COALESCE(NULLIF(trim(t.priority), ''), 'Unassigned') AS priority,
      (t."dueDate" AT TIME ZONE 'Asia/Kolkata')::date AS due_date,
      (t."completedAt" AT TIME ZONE 'Asia/Kolkata')::date AS done_date,
      t."completedAt",
      t."createdAt"
  FROM followup_tasks t
  JOIN base b
    ON b.lead_id = t.lead_id
),

open_task AS (
  SELECT DISTINCT ON (lead_id)
         lead_id,
         due_date,
         outcome,
         priority
  FROM tasks
  WHERE status = 'OPEN'
  ORDER BY lead_id, due_date
),

last_task AS (
  SELECT DISTINCT ON (lead_id)
         lead_id,
         due_date AS next_follow_up_date_any
  FROM tasks
  ORDER BY lead_id, "createdAt" DESC
),

task_stats AS (
  SELECT
      lead_id,
      count(*) AS followup_tasks,

      count(*) FILTER (
        WHERE outcome = 'dnp'
          AND owner_role IN ('TL','IS')
      ) AS tl_is_dnp_tasks,

      count(*) FILTER (
        WHERE status = 'DONE'
      ) AS followups_done_all_time,

      count(*) FILTER (
        WHERE status = 'DONE'
          AND "completedAt" >= (
            CAST({{start_date}} AS date)::timestamp
            AT TIME ZONE 'Asia/Kolkata'
          )
          AND "completedAt" < (
            (CAST({{end_date}} AS date) + 1)::timestamp
            AT TIME ZONE 'Asia/Kolkata'
          )
      ) AS followups_done_in_range,

      count(*) FILTER (
        WHERE status = 'DONE'
          AND "completedAt" >= (
            CAST({{start_date}} AS date)::timestamp
            AT TIME ZONE 'Asia/Kolkata'
          )
          AND "completedAt" < (
            (CAST({{end_date}} AS date) + 1)::timestamp
            AT TIME ZONE 'Asia/Kolkata'
          )
          AND due_date IS NOT NULL
          AND done_date > due_date
      ) AS followups_done_overdue

  FROM tasks
  GROUP BY 1
),

-- ===== follow-up tracker additions: FU1-FU6 + latest =====

fresh_meeting AS (
  -- latest fresh_meeting per lead, gives the true FU1 due-date anchor
  SELECT DISTINCT ON (h.lead_id)
      h.lead_id,
      (h.meeting_done_date AT TIME ZONE 'Asia/Kolkata')::date + 2
        AS fu1_due_date
  FROM meeting_metrics_history h
  JOIN base b
    ON b.lead_id = h.lead_id
  WHERE h.meeting_type = 'fresh_meeting'
  ORDER BY h.lead_id, h.meeting_schedule_date DESC
),

fu_seqd AS (
  -- every followup_tasks row for the lead, numbered in creation order
  SELECT
      t.lead_id,
      t.status,
      t."sourceOutcome" AS raw_outcome,
      t."dueDate",
      t."createdAt",
      t."completedAt",
      row_number() OVER (
        PARTITION BY t.lead_id
        ORDER BY t."createdAt" ASC
      ) AS fu_seq,
      count(*) OVER (PARTITION BY t.lead_id) AS fu_total
  FROM followup_tasks t
  JOIN base b
    ON b.lead_id = t.lead_id
),

fu_ranked AS (
  -- resolve effective due date (FU1 overridden from fresh_meeting) and
  -- suppress outcome whenever the task is still OPEN
  SELECT
      f.lead_id,
      f.status,
      CASE WHEN f.status = 'OPEN' THEN NULL ELSE f.raw_outcome END
        AS outcome,
      f.fu_seq,
      f.fu_total,

      CASE
        WHEN f.fu_seq = 1 THEN fm.fu1_due_date
        ELSE (f."dueDate" AT TIME ZONE 'Asia/Kolkata')::date
      END AS effective_due_date,

      (f."createdAt" AT TIME ZONE 'Asia/Kolkata')::date AS created_date,
      (f."completedAt" AT TIME ZONE 'Asia/Kolkata')::date AS completed_date

  FROM fu_seqd f
  LEFT JOIN fresh_meeting fm
    ON fm.lead_id = f.lead_id
),

fu_overdue AS (
  -- overdue in days:
  --   DONE            -> completedAt - due_date (floored at 0)
  --   OPEN, FU1       -> createdAt   - due_date (floored at 0)
  --   OPEN, FU2+      -> as_of       - due_date (floored at 0)
  SELECT
      r.lead_id,
      r.status,
      r.outcome,
      r.fu_seq,
      r.fu_total,
      r.completed_date,
      r.effective_due_date AS due_date,

      CASE
        WHEN r.effective_due_date IS NULL
          THEN NULL
        WHEN r.status = 'DONE' AND r.completed_date IS NOT NULL
          THEN GREATEST(r.completed_date - r.effective_due_date, 0)
        WHEN r.status = 'DONE' AND r.completed_date IS NULL
          THEN NULL
        WHEN r.fu_seq = 1 AND r.status = 'OPEN'
          THEN GREATEST(r.created_date - r.effective_due_date, 0)
        WHEN r.fu_seq > 1 AND r.status = 'OPEN'
          THEN GREATEST(b.as_of - r.effective_due_date, 0)
        ELSE NULL
      END AS overdue_days

  FROM fu_ranked r
  JOIN base b
    ON b.lead_id = r.lead_id
),

fu_pivot AS (
  SELECT
      lead_id,

      MAX(CASE WHEN fu_seq = 1 THEN status         END) AS fu1_status,
      MAX(CASE WHEN fu_seq = 1 THEN outcome        END) AS fu1_outcome,
      MAX(CASE WHEN fu_seq = 1 THEN overdue_days   END) AS fu1_overdue,
      MAX(CASE WHEN fu_seq = 1 THEN completed_date END) AS fu1_completed_at,
      MAX(CASE WHEN fu_seq = 1 THEN due_date       END) AS fu1_due_date,

      MAX(CASE WHEN fu_seq = 2 THEN status         END) AS fu2_status,
      MAX(CASE WHEN fu_seq = 2 THEN outcome        END) AS fu2_outcome,
      MAX(CASE WHEN fu_seq = 2 THEN overdue_days   END) AS fu2_overdue,
      MAX(CASE WHEN fu_seq = 2 THEN completed_date END) AS fu2_completed_at,
      MAX(CASE WHEN fu_seq = 2 THEN due_date       END) AS fu2_due_date,

      MAX(CASE WHEN fu_seq = 3 THEN status         END) AS fu3_status,
      MAX(CASE WHEN fu_seq = 3 THEN outcome        END) AS fu3_outcome,
      MAX(CASE WHEN fu_seq = 3 THEN overdue_days   END) AS fu3_overdue,
      MAX(CASE WHEN fu_seq = 3 THEN completed_date END) AS fu3_completed_at,
      MAX(CASE WHEN fu_seq = 3 THEN due_date       END) AS fu3_due_date,

      MAX(CASE WHEN fu_seq = 4 THEN status         END) AS fu4_status,
      MAX(CASE WHEN fu_seq = 4 THEN outcome        END) AS fu4_outcome,
      MAX(CASE WHEN fu_seq = 4 THEN overdue_days   END) AS fu4_overdue,
      MAX(CASE WHEN fu_seq = 4 THEN completed_date END) AS fu4_completed_at,
      MAX(CASE WHEN fu_seq = 4 THEN due_date       END) AS fu4_due_date,

      MAX(CASE WHEN fu_seq = 5 THEN status         END) AS fu5_status,
      MAX(CASE WHEN fu_seq = 5 THEN outcome        END) AS fu5_outcome,
      MAX(CASE WHEN fu_seq = 5 THEN overdue_days   END) AS fu5_overdue,
      MAX(CASE WHEN fu_seq = 5 THEN completed_date END) AS fu5_completed_at,
      MAX(CASE WHEN fu_seq = 5 THEN due_date       END) AS fu5_due_date,

      MAX(CASE WHEN fu_seq = 6 THEN status         END) AS fu6_status,
      MAX(CASE WHEN fu_seq = 6 THEN outcome        END) AS fu6_outcome,
      MAX(CASE WHEN fu_seq = 6 THEN overdue_days   END) AS fu6_overdue,
      MAX(CASE WHEN fu_seq = 6 THEN completed_date END) AS fu6_completed_at,
      MAX(CASE WHEN fu_seq = 6 THEN due_date       END) AS fu6_due_date,

      MAX(CASE WHEN fu_seq = fu_total AND fu_total > 6
               THEN status END)         AS latest_status,
      MAX(CASE WHEN fu_seq = fu_total AND fu_total > 6
               THEN outcome END)        AS latest_outcome,
      MAX(CASE WHEN fu_seq = fu_total AND fu_total > 6
               THEN overdue_days END)   AS latest_overdue,
      MAX(CASE WHEN fu_seq = fu_total AND fu_total > 6
               THEN completed_date END) AS latest_completed_at,
      MAX(CASE WHEN fu_seq = fu_total AND fu_total > 6
               THEN due_date END)       AS latest_due_date,

      -- flags scoped to FU1-FU6 only, "today" taken from the DB's own
      -- current date (not as_of / report end_date)
      MAX(CASE
            WHEN fu_seq <= 6
             AND completed_date = (now() AT TIME ZONE 'Asia/Kolkata')::date
              THEN 1 ELSE 0
          END) AS fu_completedat_today,

      MAX(CASE
            WHEN fu_seq <= 6
             AND due_date = (now() AT TIME ZONE 'Asia/Kolkata')::date
              THEN 1 ELSE 0
          END) AS fu_due_date_today

  FROM fu_overdue
  GROUP BY lead_id
)

-- ===== end follow-up tracker additions =====

SELECT
  b.lead_id,

  CASE
    WHEN d.disposition_id IS NOT NULL
      THEN 'Processed'
    ELSE 'Not Processed'
  END AS disposition_status,

  d.disposition_id,

  b.sc_channel,

  d.meta_sales_channel AS "Sales Channel (FS/IS)",

  b.lighthouse_status AS "Current Status (Lighthouse)",

  b.cluster,

  m.meeting_city,

  b.city AS site_city,

  b.zsm_name AS zsm,
  b.tl_name AS tl,
  b.sc_name AS sc,

  b.lead_status,

  (b.order_closure_datetime AT TIME ZONE 'Asia/Kolkata')
    AS order_closure_datetime_ist,

  b.first_meeting_done_date,

  m.meeting_schedule_date AS meeting_date,
  m.meeting_done_date,

  mc.meetings_in_range,
  mc.notes_submitted_in_range,

  m.disposition_status AS note_status,

  (m.disposition_status = 'COMPLETED')
    AS note_submitted,

  m.note_submitted_at_ist,

  (m.audio_path ~* '^https?://')
    AS audio_present,

  m.audio_path,
  m.audio_duration_sec,

  CASE
    WHEN m.audio_duration_sec < 45
      THEN 'Under 45 sec'
    WHEN m.audio_duration_sec >= 45
      THEN '45 sec and above'
  END AS duration_band,

  COALESCE(qa.dispositions_missing, 11)
    AS dispositions_missing,

  (
    (m.audio_path ~* '^https?://')
    AND COALESCE(qa.dispositions_missing, 11) = 0
  ) AS clean_note,

  d.overall_disposition,
  d.summary,

  qa.q_timeline AS "Timeline to go Solar",
  qa.q_bill AS "Bill Value",
  qa.q_dm AS "Decision Maker",
  qa.q_competitor AS "Competitor Quote",
  qa.q_objection AS "Main Objection",
  qa.q_site_visit AS "Site Visit Done?",
  qa.q_price AS "Quoted Price",
  qa.q_buffer AS "Buffer / Margin Left",
  qa.q_next_fu AS "Next Follow up (Voice Note)",
  qa.q_reading AS "SC's Reading",
  qa.q_offering AS "Offering",

  fs."lastContactAt" AT TIME ZONE 'Asia/Kolkata'
    AS last_follow_up_date,

  fs."lastOutcome" AS last_follow_up_status,

  COALESCE(
    ot.due_date,
    lt.next_follow_up_date_any
  ) AS next_follow_up_date,

  ot.outcome AS open_task_outcome,

  COALESCE(
    ot.priority,
    'Unassigned'
  ) AS priority,

  COALESCE(
    ts.followup_tasks,
    0
  ) AS followup_tasks,

  COALESCE(
    ts.followups_done_all_time,
    0
  ) AS total_followup_done,

  COALESCE(
    ts.followups_done_in_range,
    0
  ) AS followups_done_in_range,

  COALESCE(
    ts.followups_done_overdue,
    0
  ) AS followups_done_overdue,

  COALESCE(
    ts.tl_is_dnp_tasks,
    0
  ) AS tl_is_dnp_tasks,

  fs."exitReason" AS exit_reason,

  b.is_field_sale_sc,
  b.tl_resolved,
  b.is_on_spot,

  (
    b.is_field_sale_sc
    AND b.tl_resolved
    AND NOT b.is_on_spot
  ) AS in_review_cohort,

  CASE
    WHEN (
      fs."exitedAt" IS NOT NULL
      OR fs."exitReason" IS NOT NULL
    )
      THEN 'terminal'

    WHEN COALESCE(ts.tl_is_dnp_tasks, 0) >= 3
      THEN 'customer_unreachable'

    WHEN COALESCE(
      ot.due_date,
      CASE
        WHEN COALESCE(ts.followup_tasks, 0) = 0
          THEN b.first_meeting_done_date + 2
      END
    ) IS NULL
      THEN 'task_gap'

    WHEN COALESCE(ts.followup_tasks, 0) = 0
      AND b.is_booked
      THEN 'booked'

    WHEN COALESCE(ts.followup_tasks, 0) = 0
      AND b.is_lost
      THEN 'lost'

    WHEN COALESCE(
      ot.due_date,
      b.first_meeting_done_date + 2
    ) = b.as_of
      THEN 'today'

    WHEN COALESCE(
      ot.due_date,
      b.first_meeting_done_date + 2
    ) = b.as_of + 1
      THEN 'tomorrow'

    WHEN COALESCE(
      ot.due_date,
      b.first_meeting_done_date + 2
    ) BETWEEN b.as_of + 2 AND b.as_of + 7
      THEN 'this_week'

    WHEN COALESCE(
      ot.due_date,
      b.first_meeting_done_date + 2
    ) < b.as_of
      THEN 'overdue'

    ELSE 'later'
  END AS funnel_bucket,

  COALESCE(
    ot.due_date,
    CASE
      WHEN COALESCE(ts.followup_tasks, 0) = 0
        THEN b.first_meeting_done_date + 2
    END
  ) AS followup_due_date,

  CASE
    WHEN COALESCE(
      ot.due_date,
      CASE
        WHEN COALESCE(ts.followup_tasks, 0) = 0
          THEN b.first_meeting_done_date + 2
      END
    ) < b.as_of
      THEN b.as_of - COALESCE(
        ot.due_date,
        b.first_meeting_done_date + 2
      )
  END AS overdue_days,

  b.as_of AS snapshot_date,

  -- ===== follow-up tracker columns =====

  fp.fu1_status, fp.fu1_outcome, fp.fu1_overdue, fp.fu1_completed_at, fp.fu1_due_date,
  fp.fu2_status, fp.fu2_outcome, fp.fu2_overdue, fp.fu2_completed_at, fp.fu2_due_date,
  fp.fu3_status, fp.fu3_outcome, fp.fu3_overdue, fp.fu3_completed_at, fp.fu3_due_date,
  fp.fu4_status, fp.fu4_outcome, fp.fu4_overdue, fp.fu4_completed_at, fp.fu4_due_date,
  fp.fu5_status, fp.fu5_outcome, fp.fu5_overdue, fp.fu5_completed_at, fp.fu5_due_date,
  fp.fu6_status, fp.fu6_outcome, fp.fu6_overdue, fp.fu6_completed_at, fp.fu6_due_date,

  fp.latest_status, fp.latest_outcome, fp.latest_overdue,
  fp.latest_completed_at, fp.latest_due_date,

  fp.fu_completedat_today,
  fp.fu_due_date_today

FROM base b

LEFT JOIN mtg m
  ON m.lead_id = b.lead_id

LEFT JOIN mtg_counts mc
  ON mc.lead_id = b.lead_id

LEFT JOIN disp d
  ON d.lead_id = b.lead_id

LEFT JOIN qa
  ON qa.rid = d.disposition_id

LEFT JOIN followup_state fs
  ON fs.lead_id = b.lead_id

LEFT JOIN open_task ot
  ON ot.lead_id = b.lead_id

LEFT JOIN last_task lt
  ON lt.lead_id = b.lead_id

LEFT JOIN task_stats ts
  ON ts.lead_id = b.lead_id

LEFT JOIN fu_pivot fp
  ON fp.lead_id = b.lead_id

ORDER BY
    b.city,
    b.tl_name,
    b.sc_name,
    b.lead_id;

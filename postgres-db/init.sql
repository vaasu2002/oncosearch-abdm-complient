-- ABDM Mock Database Schema + Seed Data

CREATE TABLE IF NOT EXISTS patients (
    id              SERIAL PRIMARY KEY,
    abha_address    VARCHAR(100) UNIQUE NOT NULL,
    name            VARCHAR(200) NOT NULL,
    gender          VARCHAR(10) NOT NULL,
    birth_date      DATE NOT NULL,
    phone           VARCHAR(20),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS conditions (
    id              SERIAL PRIMARY KEY,
    patient_id      INTEGER REFERENCES patients(id) ON DELETE CASCADE,
    icd10_code      VARCHAR(20) NOT NULL, -- International Classification of Diseases, 10th Revision, Clinical Modification
    display         TEXT NOT NULL,
    clinical_status VARCHAR(50) DEFAULT 'active',
    onset_date      DATE,
    recorded_date   DATE DEFAULT CURRENT_DATE
);

CREATE TABLE IF NOT EXISTS observations (
    id              SERIAL PRIMARY KEY,
    patient_id      INTEGER REFERENCES patients(id) ON DELETE CASCADE,
    loinc_code      VARCHAR(20), -- Logical Observation Identifiers Names and Codes
    display         TEXT NOT NULL,
    value_string    TEXT,
    interpretation VARCHAR(50),
    effective_date  DATE DEFAULT CURRENT_DATE
);

CREATE TABLE IF NOT EXISTS medication_statements (
    id              SERIAL PRIMARY KEY,
    patient_id      INTEGER REFERENCES patients(id) ON DELETE CASCADE,
    rxnorm_code     VARCHAR(20), -- RxNorm codes (normalize names for clinical drugs, strengths, and dose forms)
    medication_name TEXT NOT NULL,
    status          VARCHAR(50) DEFAULT 'completed',
    effective_start DATE,
    effective_end   DATE,
    dosage_text     TEXT
);

-- ============================================================
-- Patient 1: Arjun Mehta — NSCLC with EGFR mutation
-- ============================================================
INSERT INTO patients (abha_address, name, gender, birth_date, phone)
VALUES ('arjun.mehta@abdm', 'Arjun Mehta', 'male', '1968-03-15', '+91-9876543210');

INSERT INTO conditions (patient_id, icd10_code, display, clinical_status, onset_date)
VALUES (
    (SELECT id FROM patients WHERE abha_address = 'arjun.mehta@abdm'),
    'C34.1',
    'Non-small cell lung cancer, upper lobe',
    'active',
    '2022-11-10'
);

INSERT INTO observations (patient_id, loinc_code, display, value_string, interpretation, effective_date)
VALUES
    (
        (SELECT id FROM patients WHERE abha_address = 'arjun.mehta@abdm'),
        '55233-1',
        'EGFR gene mutation analysis',
        'EGFR Exon 19 deletion detected (p.E746_A750del)',
        'positive',
        '2022-11-20'
    ),
    (
        (SELECT id FROM patients WHERE abha_address = 'arjun.mehta@abdm'),
        '81704-9',
        'PD-L1 expression (TPS)',
        'TPS < 1% (Negative)',
        'negative',
        '2022-11-20'
    ),
    (
        (SELECT id FROM patients WHERE abha_address = 'arjun.mehta@abdm'),
        '21907-1',
        'ECOG Performance Status',
        'ECOG 1 — Restricted in physically strenuous activity but ambulatory',
        'normal',
        '2023-01-05'
    );

INSERT INTO medication_statements (patient_id, rxnorm_code, medication_name, status, effective_start, effective_end, dosage_text)
VALUES
    (
        (SELECT id FROM patients WHERE abha_address = 'arjun.mehta@abdm'),
        '1860483',
        'Osimertinib 80mg',
        'active',
        '2022-12-01',
        NULL,
        '80 mg orally once daily'
    ),
    (
        (SELECT id FROM patients WHERE abha_address = 'arjun.mehta@abdm'),
        '1860492',
        'Gefitinib 250mg',
        'completed',
        '2022-11-25',
        '2022-11-30',
        '250 mg orally once daily (switched to Osimertinib due to tolerability)'
    );

-- ============================================================
-- Patient 2: Priya Sharma — Breast cancer with BRCA1 mutation
-- ============================================================
INSERT INTO patients (abha_address, name, gender, birth_date, phone)
VALUES ('priya.sharma@abdm', 'Priya Sharma', 'female', '1975-07-22', '+91-9123456780');

INSERT INTO conditions (patient_id, icd10_code, display, clinical_status, onset_date)
VALUES
    (
        (SELECT id FROM patients WHERE abha_address = 'priya.sharma@abdm'),
        'C50.911',
        'Malignant neoplasm of unspecified site of right female breast',
        'active',
        '2021-05-14'
    ),
    (
        (SELECT id FROM patients WHERE abha_address = 'priya.sharma@abdm'),
        'Z15.01',
        'Genetic susceptibility to malignant neoplasm of breast (BRCA1)',
        'active',
        '2021-06-02'
    );

INSERT INTO observations (patient_id, loinc_code, display, value_string, interpretation, effective_date)
VALUES
    (
        (SELECT id FROM patients WHERE abha_address = 'priya.sharma@abdm'),
        '21637-4',
        'BRCA1 gene mutation analysis',
        'BRCA1 pathogenic variant detected: c.5266dupC (p.Gln1756Profs*74)',
        'positive',
        '2021-06-02'
    ),
    (
        (SELECT id FROM patients WHERE abha_address = 'priya.sharma@abdm'),
        '85319-2',
        'HER2 [Presence] in Breast cancer specimen by Immune stain',
        'HER2 Negative (Score 1+)',
        'negative',
        '2021-05-20'
    ),
    (
        (SELECT id FROM patients WHERE abha_address = 'priya.sharma@abdm'),
        '85310-1',
        'Estrogen receptor Ag [Presence] in Breast cancer specimen',
        'ER Positive (Allred Score 7/8)',
        'positive',
        '2021-05-20'
    ),
    (
        (SELECT id FROM patients WHERE abha_address = 'priya.sharma@abdm'),
        '85325-9',
        'Progesterone receptor Ag [Presence] in Breast cancer specimen',
        'PR Positive (Allred Score 6/8)',
        'positive',
        '2021-05-20'
    );

INSERT INTO medication_statements (patient_id, rxnorm_code, medication_name, status, effective_start, effective_end, dosage_text)
VALUES
    (
        (SELECT id FROM patients WHERE abha_address = 'priya.sharma@abdm'),
        '2049106',
        'Olaparib 150mg',
        'active',
        '2021-09-01',
        NULL,
        '300 mg (two 150 mg tablets) orally twice daily'
    ),
    (
        (SELECT id FROM patients WHERE abha_address = 'priya.sharma@abdm'),
        '1860484',
        'Pembrolizumab 200mg IV',
        'completed',
        '2021-07-15',
        '2021-08-25',
        '200 mg IV infusion every 3 weeks (4 cycles, completed)'
    ),
    (
        (SELECT id FROM patients WHERE abha_address = 'priya.sharma@abdm'),
        '203160',
        'Tamoxifen 20mg',
        'active',
        '2022-01-10',
        NULL,
        '20 mg orally once daily'
    );

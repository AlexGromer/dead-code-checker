package com.acme.shop;

public class Auditor {
    private final PriceService audited;

    public Auditor(PriceService audited) {
        this.audited = audited;
    }

    public long total(String sku) {
        return audited.price(sku);
    }
}

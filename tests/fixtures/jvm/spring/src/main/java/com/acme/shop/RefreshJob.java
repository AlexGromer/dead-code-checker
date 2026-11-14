package com.acme.shop;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
public class RefreshJob {
    @Autowired
    private PriceService warm;

    @Scheduled(fixedRate = 60000)
    public void refresh() {
        warm.price("warmup");
    }
}
